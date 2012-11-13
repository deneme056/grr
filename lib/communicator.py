#!/usr/bin/env python
# Copyright 2010 Google Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Abstracts encryption and authentication."""


import hashlib
import time
import zlib


from M2Crypto import BIO
from M2Crypto import EVP
from M2Crypto import Rand
from M2Crypto import RSA
from M2Crypto import X509

from google.protobuf import message
from grr.client import conf as flags
from grr.lib import registry
from grr.lib import stats
from grr.lib import utils
from grr.proto import jobs_pb2

flags.DEFINE_string("compression", default="ZCOMPRESS",
                    help="Type of compression (ZCOMPRESS, UNCOMPRESSED)")

FLAGS = flags.FLAGS

# Constants.
ENCRYPT = 1
DECRYPT = 0


class CommunicatorInit(registry.InitHook):

  pre = ["StatsInit"]

  def RunOnce(self):
    """This is run only once."""
    # Initialize the PRNG.
    Rand.rand_seed(Rand.rand_bytes(1000))

    # Counters used here
    stats.STATS.RegisterVar("grr_client_unknown")
    stats.STATS.RegisterVar("grr_decoding_error")
    stats.STATS.RegisterVar("grr_decryption_error")
    stats.STATS.RegisterVar("grr_rekey_error")
    stats.STATS.RegisterVar("grr_authenticated_messages")
    stats.STATS.RegisterVar("grr_unauthenticated_messages")
    stats.STATS.RegisterVar("grr_rsa_operations")


class Error(stats.CountingExceptionMixin, Exception):
  """Base class for all exceptions in this module."""
  pass


class DecodingError(Error):
  """Raised when the message failed to decrypt or decompress."""
  counter = "grr_decoding_error"


class DecryptionError(DecodingError):
  """Raised when the message can not be decrypted properly."""
  counter = "grr_decryption_error"


class RekeyError(DecodingError):
  """Raised when the session key is not known and rekeying is needed."""
  counter = "grr_rekey_error"


class UnknownClientCert(DecodingError):
  """Raised when the client key is not retrieved."""
  counter = "grr_client_unknown"


class PubKeyCache(object):
  """A cache of public keys for different destinations."""

  def __init__(self):
    self.pub_key_cache = utils.FastStore(max_size=50000)

  @staticmethod
  def GetCNFromCert(cert):
    subject = cert.get_subject()
    try:
      cn_id = subject.nid["CN"]
      cn = subject.get_entries_by_nid(cn_id)[0]
    except IndexError:
      raise IOError("Cert has no CN")

    return cn.get_data().as_text()

  @staticmethod
  def PubKeyFromCert(cert):
    pub_key = cert.get_pubkey().get_rsa()
    bio = BIO.MemoryBuffer()
    pub_key.save_pub_key_bio(bio)

    return bio.read_all()

  def Flush(self):
    """Flushes the cert cache."""
    self.pub_key_cache.Flush()

  def Put(self, destination, pub_key):
    self.pub_key_cache.Put(destination, pub_key)

  def GetRSAPublicKey(self, common_name="Server"):
    """Retrieve the relevant public key for that common name.

    This maintains a cache of public keys or loads them from external
    sources if available.

    Args:
      common_name: The common_name of the key we need.

    Returns:
      A valid public key.
    """
    try:
      pub_key = self.pub_key_cache.Get(common_name)
      bio = BIO.MemoryBuffer(pub_key)
      return RSA.load_pub_key_bio(bio)
    except (KeyError, X509.X509Error):
      raise KeyError("No certificate found")


class Cipher(object):
  """Holds keying information."""
  hash_function = hashlib.sha256
  hash_function_name = "sha256"
  cipher_name = "aes_128_cbc"
  key_size = 128
  iv_size = 128
  e_padding = RSA.pkcs1_oaep_padding

  # These fields get filled in by the constructor
  private_key = None
  cipher = None
  cipher_metadata = None
  encrypted_cipher = None
  encrypted_cipher_metadata = None

  def __init__(self, source, destination, private_key, pub_key_cache):
    self.private_key = private_key

    self.cipher = jobs_pb2.CipherProperties(
        name=self.cipher_name,
        key=Rand.rand_pseudo_bytes(self.key_size / 8)[0],
        iv=Rand.rand_pseudo_bytes(self.iv_size / 8)[0],
        hmac_key=Rand.rand_pseudo_bytes(self.key_size / 8)[0],
        )

    self.pub_key_cache = pub_key_cache
    serialized_cipher = self.cipher.SerializeToString()

    self.cipher_metadata = jobs_pb2.CipherMetadata(source=source)

    # Sign this cipher.
    digest = self.hash_function(serialized_cipher).digest()

    # We never want to have a password dialog
    private_key = RSA.load_key_string(self.private_key, callback=lambda x: "")
    self.cipher_metadata.signature = private_key.sign(
        digest, self.hash_function_name)

    # Now encrypt the cipher with our key
    rsa_key = pub_key_cache.GetRSAPublicKey(destination)

    stats.STATS.Increment("grr_rsa_operations")
    self.encrypted_cipher = rsa_key.public_encrypt(
        serialized_cipher, self.e_padding)

    # Encrypt the metadata block symmetrically.
    _, self.encrypted_cipher_metadata = self.Encrypt(
        self.cipher_metadata.SerializeToString(), self.cipher.iv)

    self.signature_verified = True

  def Encrypt(self, data, iv=None):
    """Symmetrically encrypt the data using the optional iv."""
    if iv is None:
      iv = Rand.rand_pseudo_bytes(self.iv_size / 8)[0]

    evp_cipher = EVP.Cipher(alg=self.cipher_name, key=self.cipher.key,
                            iv=iv, op=ENCRYPT)

    ctext = evp_cipher.update(data)
    ctext += evp_cipher.final()

    return iv, ctext

  def Decrypt(self, data, iv):
    evp_cipher = EVP.Cipher(alg=self.cipher_name, key=self.cipher.key,
                            iv=iv, op=DECRYPT)

    text = evp_cipher.update(data)
    text += evp_cipher.final()

    return text

  def HMAC(self, data):
    hmac = EVP.HMAC(self.cipher.hmac_key, algo="sha1")
    hmac.update(data)
    return hmac.final()


class ReceivedCipher(Cipher):
  """A cipher which we received from our peer."""

  # Indicates if the cipher contained in the response_comms is verified.
  signature_verified = False

  # pylint: disable=W0231
  def __init__(self, response_comms, private_key, pub_key_cache):
    self.private_key = private_key
    self.pub_key_cache = pub_key_cache

    # Decrypt the message
    private_key = RSA.load_key_string(self.private_key, callback=lambda x: "")
    try:
      self.encrypted_cipher = response_comms.encrypted_cipher
      self.serialized_cipher = private_key.private_decrypt(
          response_comms.encrypted_cipher, self.e_padding)

      self.cipher = jobs_pb2.CipherProperties()
      self.cipher.ParseFromString(self.serialized_cipher)

      # Check the key lengths.
      if (len(self.cipher.key) != self.key_size / 8 or
          len(self.cipher.iv) != self.iv_size / 8):
        raise DecryptionError("Invalid cipher.")

      if response_comms.api_version >= 3:
        if len(self.cipher.hmac_key) != self.key_size / 8:
          raise DecryptionError("Invalid cipher.")

        # New version: cipher_metadata contains information about the cipher.
        # Decrypt the metadata symmetrically
        self.encrypted_cipher_metadata = (
            response_comms.encrypted_cipher_metadata)
        self.cipher_metadata = jobs_pb2.CipherMetadata()
        self.cipher_metadata.ParseFromString(self.Decrypt(
            response_comms.encrypted_cipher_metadata, self.cipher.iv))

        self.VerifyCipherSignature()
      else:
        # Old version: To be set once the message is verified.
        self.cipher_metadata = None
    except RSA.RSAError as e:
      raise DecryptionError(e)

  def VerifyCipherSignature(self):
    """Verify the signature on the encrypted cipher block."""
    if self.cipher_metadata.signature:
      digest = self.hash_function(self.serialized_cipher).digest()
      try:
        remote_public_key = self.pub_key_cache.GetRSAPublicKey(
            self.cipher_metadata.source)

        stats.STATS.Increment("grr_rsa_operations")
        if remote_public_key.verify(digest, self.cipher_metadata.signature,
                                    self.hash_function_name):
          self.signature_verified = True

      except (UnknownClientCert, X509.X509Error):
        pass


class Communicator(object):
  """A class responsible for encoding and decoding comms."""
  server_name = None

  def __init__(self, certificate):
    """Creates a communicator.

    Args:
       certificate: Our own certificate and key in string form (as PEM).
    """
    # A cache of cipher objects.
    self.cipher_cache = utils.TimeBasedCache()
    self.private_key = certificate

    # A cache for encrypted ciphers
    self.encrypted_cipher_cache = utils.FastStore(max_size=50000)

    # A cache of public keys
    self.pub_key_cache = PubKeyCache()
    self._LoadOurCertificate(certificate)

  def _LoadOurCertificate(self, certificate):
    self.cert = X509.load_cert_string(certificate)

    # This is our private key - make sure it has no password set.
    self.private_key = certificate

    # Make sure its valid
    RSA.load_key_string(certificate, callback=lambda x: "")

    # Our common name
    self.common_name = PubKeyCache.GetCNFromCert(self.cert)

    # Make sure we know about our own public key
    self.pub_key_cache.Put(
        self.common_name, self.pub_key_cache.PubKeyFromCert(self.cert))

  def EncodeMessageList(self, message_list, signed_message_list):
    """Encode the MessageList proto into the signed_message_list proto."""
    # By default uncompress
    uncompressed_data = message_list.SerializeToString()
    signed_message_list.message_list = uncompressed_data

    if FLAGS.compression == "ZCOMPRESS":
      compressed_data = zlib.compress(uncompressed_data)

      # Only compress if it buys us something.
      if len(compressed_data) < len(uncompressed_data):
        signed_message_list.compression = (
            jobs_pb2.SignedMessageList.ZCOMPRESSION)
        signed_message_list.message_list = compressed_data

  def EncodeMessages(self, message_list, result, destination=None,
                     timestamp=None, api_version=2):
    """Accepts a list of messages and encodes for transmission.

    This function signs and then encrypts the payload.

    Args:
       message_list: A MessageList protobuf containing a list of
       GrrMessages.

       result: A ClientCommunication protobuf which will be filled in.

       destination: The CN of the remote system this should go to.

       timestamp: A timestamp to use for the signed messages. If None - use the
              current time.

       api_version: The api version which this should be encoded in.

    Returns:
       A nonce (based on time) which is inserted to the encrypted payload. The
       client can verify that the server is able to decrypt the message and
       return the nonce.

    Raises:
       RuntimeError: If we do not support this api version.
    """
    if api_version not in [2, 3]:
      raise RuntimeError("Unsupported api version.")

    if destination is None:
      destination = self.server_name

    # Make a nonce for this transaction
    if timestamp is None:
      self.timestamp = timestamp = long(time.time() * 1000000)

    # Do we have a cached cipher to talk to this destination?
    try:
      cipher = self.cipher_cache.Get(destination)
    except KeyError:
      # Make a new one
      cipher = Cipher(self.common_name, destination, self.private_key,
                      self.pub_key_cache)
      self.cipher_cache.Put(destination, cipher)

    signed_message_list = jobs_pb2.SignedMessageList(timestamp=timestamp)
    self.EncodeMessageList(message_list, signed_message_list)

    # TODO(user): This is for backwards compatibility. Remove when all
    # clients are moved to new scheme.
    if api_version == 2:
      signed_message_list.source = self.common_name

      # Old scheme - message list is signed.
      digest = cipher.hash_function(signed_message_list.message_list).digest()

      # We never want to have a password dialog
      private_key = RSA.load_key_string(self.private_key, callback=lambda x: "")
      signed_message_list.signature = private_key.sign(
          digest, cipher.hash_function_name)

    elif api_version == 3:
      result.encrypted_cipher_metadata = cipher.encrypted_cipher_metadata

    # Include the encrypted cipher.
    result.encrypted_cipher = cipher.encrypted_cipher

    serialized_message_list = signed_message_list.SerializeToString()

    # Encrypt the message symmetrically.
    if api_version >= 3:
      # New scheme cipher is signed plus hmac over message list.
      result.iv, result.encrypted = cipher.Encrypt(serialized_message_list)
      result.hmac = cipher.HMAC(result.encrypted)
    else:
      _, result.encrypted = cipher.Encrypt(serialized_message_list,
                                           cipher.cipher.iv)

    result.api_version = api_version

    return timestamp

  def DecryptMessage(self, encrypted_response):
    """Decrypt the serialized, encrypted string.

    Args:
       encrypted_response: A serialized and encrypted string.

    Returns:
       a Signed_Message_List protobuf
    """
    response_comms = jobs_pb2.ClientCommunication()
    response_comms.ParseFromString(encrypted_response)

    return self.DecodeMessages(response_comms)

  def DecompressMessageList(self, signed_message_list):
    """Decompress the message data from signed_message_list.

    Args:
      signed_message_list: A SignedMessageList proto with some data in it.

    Returns:
      a MessageList proto.

    Raises:
      DecodingError: If decompression fails.
    """
    compression = signed_message_list.compression
    if compression == jobs_pb2.SignedMessageList.UNCOMPRESSED:
      data = signed_message_list.message_list

    elif compression == jobs_pb2.SignedMessageList.ZCOMPRESSION:
      try:
        data = zlib.decompress(signed_message_list.message_list)
      except zlib.error as e:
        raise DecodingError("Failed to decompress: %s" % e)
    else:
      raise DecodingError("Compression scheme not supported")

    try:
      result = jobs_pb2.MessageList()
      result.ParseFromString(data)
    except message.DecodeError:
      raise DecodingError("Proto parsing failed.")

    return result

  def DecodeMessages(self, response_comms):
    """Extract and verify server message.

    Args:
        response_comms: A ClientCommunication protobuf

    Returns:
       list of messages and the CN where they came from.

    Raises:
       DecryptionError: If the message failed to decrypt properly.
    """
    if response_comms.api_version not in [2, 3]:
      raise DecryptionError("Unsupported api version.")

    if response_comms.encrypted_cipher:
      # Have we seen this cipher before?
      try:
        cipher = self.encrypted_cipher_cache.Get(
            response_comms.encrypted_cipher)
      except KeyError:
        cipher = ReceivedCipher(response_comms, self.private_key,
                                self.pub_key_cache)

        if cipher.signature_verified:
          # Remember it for next time.
          self.encrypted_cipher_cache.Put(response_comms.encrypted_cipher,
                                          cipher)

      # Add entropy to the PRNG.
      Rand.rand_add(response_comms.encrypted, len(response_comms.encrypted))

      # Decrypt the messages
      iv = response_comms.iv or cipher.cipher.iv
      signed_message_list = jobs_pb2.SignedMessageList()
      signed_message_list.ParseFromString(
          cipher.Decrypt(response_comms.encrypted, iv))

      message_list = self.DecompressMessageList(signed_message_list)

    else:
      # The message is not encrypted. We do not allow unencrypted
      # messages:
      raise DecryptionError("Server response is not encrypted.")

    # Are these messages authenticated?
    auth_state = self.VerifyMessageSignature(
        response_comms, signed_message_list, cipher,
        response_comms.api_version)

    # Mark messages as authenticated and where they came from.
    for msg in message_list.job:
      msg.auth_state = auth_state
      msg.source = cipher.cipher_metadata.source

    return (message_list.job, cipher.cipher_metadata.source,
            signed_message_list.timestamp)

  def VerifyMessageSignature(self, response_comms, signed_message_list,
                             cipher, api_version):
    """Verify the message list signature.

    This is the way the messages are verified in the client.

    In the client we also check that the nonce returned by the server is correct
    (the timestamp doubles as a nonce). If the nonce fails we deem the response
    unauthenticated since it might have resulted from a replay attack.

    Args:
       response_comms: The raw response_comms protobuf.
       signed_message_list: The SignedMessageList proto from the server.
       cipher: The cipher belonging to the remote end.
       api_version: The api version we should use.

    Returns:
       a jobs_pb2.GrrMessage.AuthorizationState.

    Raises:
       DecryptionError: if the message is corrupt.
    """
    result = jobs_pb2.GrrMessage.UNAUTHENTICATED
    if api_version < 3:
      # Old version: signature is on the message_list
      digest = cipher.hash_function(
          signed_message_list.message_list).digest()

      remote_public_key = self.pub_key_cache.GetRSAPublicKey(
          signed_message_list.source)

      stats.STATS.Increment("grr_rsa_operations")
      if remote_public_key.verify(digest, signed_message_list.signature,
                                  cipher.hash_function_name):
        stats.STATS.Increment("grr_authenticated_messages")
        result = jobs_pb2.GrrMessage.AUTHENTICATED

    else:
      if cipher.HMAC(response_comms.encrypted) != response_comms.hmac:
        raise DecryptionError("HMAC verification failed.")

      # Give the cipher another chance to check its signature.
      if not cipher.signature_verified:
        cipher.VerifyCipherSignature()

      if cipher.signature_verified:
        stats.STATS.Increment("grr_authenticated_messages")
        result = jobs_pb2.GrrMessage.AUTHENTICATED

    # Check for replay attacks. We expect the server to return the same
    # timestamp nonce we sent.
    if signed_message_list.timestamp != self.timestamp:
      result = jobs_pb2.GrrMessage.UNAUTHENTICATED

    if not cipher.cipher_metadata:
      # Fake the metadata
      cipher.cipher_metadata = jobs_pb2.CipherMetadata(
          source=signed_message_list.source)

    return result