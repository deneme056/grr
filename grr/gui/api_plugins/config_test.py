#!/usr/bin/env python
"""This modules contains tests for config API handler."""



import StringIO

from grr.gui import api_test_lib
from grr.gui.api_plugins import config as config_plugin

from grr.lib import aff4
from grr.lib import config_lib
from grr.lib import flags
from grr.lib import maintenance_utils
from grr.lib import test_lib
from grr.lib import utils

from grr.lib.rdfvalues import client as rdf_client


def GetConfigMockClass(sections=None):
  """Mocks a configuration file for use by the API handler.

  Args:
    sections: A dict containing one key per config section
    with a value of a dict containing one key per config parameter name
    and a value of config parameter value. (default {})

  Returns:
    A class to be used as a config mock.
  """

  if sections is None:
    sections = {}

  missing = object()

  type_infos = []
  values = {}
  raw_values = {}
  default_values = {}

  for section_name, section in sections.iteritems():
    for parameter_name, parameter_data in section.iteritems():
      name = "%s.%s" % (section_name, parameter_name)
      descriptor = utils.DataObject(section=section_name, name=name)
      type_infos.append(descriptor)

      if "value" in parameter_data:
        values[name] = parameter_data["value"]

      if "raw_value" in parameter_data:
        raw_values[name] = parameter_data["raw_value"]

      if "default_value" in parameter_data:
        default_values[name] = parameter_data["default_value"]

  def Get(parameter, default=missing):
    try:
      return values[parameter]
    except KeyError:
      if default is missing:
        return default_values[parameter]
      return default

  def GetRaw(parameter, default=missing):
    try:
      return raw_values[parameter]
    except KeyError:
      if default is missing:
        return default_values[parameter]
      return default

  return {"Get": Get, "GetRaw": GetRaw, "type_infos": type_infos}


class ApiGetConfigHandlerTest(test_lib.GRRBaseTest):
  """Test for ApiGetConfigHandlerTest."""

  def setUp(self):
    super(ApiGetConfigHandlerTest, self).setUp()
    self.handler = config_plugin.ApiGetConfigHandler()

  def _ConfigStub(self, sections=None):
    mock = GetConfigMockClass(sections)
    config = config_lib.CONFIG
    return utils.MultiStubber((config, "GetRaw", mock["GetRaw"]),
                              (config, "Get", mock["Get"]),
                              (config, "type_infos", mock["type_infos"]))

  def _HandleConfig(self, sections):
    with self._ConfigStub(sections):
      mock_request = utils.DataObject()
      result = self.handler.Handle(mock_request)

    return result

  def _assertHandlesConfig(self, sections, expected_result):
    actual_result = self._HandleConfig(sections)
    self.assertEquals(actual_result, expected_result)

  def testHandlesEmptyConfig(self):
    self._assertHandlesConfig(None, config_plugin.ApiGetConfigResult())

  def testHandlesEmptySection(self):
    self._assertHandlesConfig({
        "section": {}
    }, config_plugin.ApiGetConfigResult())

  def testHandlesConfigOption(self):
    input_dict = {
        "section": {
            "parameter": {
                "value": u"value",
                "raw_value": u"value"
            }
        }
    }
    result = self._HandleConfig(input_dict)
    self.assertEqual(len(result.sections), 1)
    self.assertEqual(len(result.sections[0].options), 1)
    self.assertEqual(result.sections[0].options[0].name, "section.parameter")
    self.assertEqual(result.sections[0].options[0].value, "value")

  def testRendersRedacted(self):
    input_dict = {
        "Mysql": {
            "database_password": {
                "value": u"secret",
                "raw_value": u"secret"
            }
        }
    }
    result = self._HandleConfig(input_dict)
    self.assertTrue(result.sections[0].options[0].is_redacted)


class ApiGetConfigHandlerRegressionTest(
    api_test_lib.ApiCallHandlerRegressionTest):

  api_method = "GetConfig"
  handler = config_plugin.ApiGetConfigHandler

  def Run(self):
    config_obj = config_lib.GrrConfigManager()
    config_obj.DEFINE_bool("SectionFoo.sample_boolean_option", True,
                           "Regression test sample boolean option.")
    config_obj.DEFINE_integer("SectionFoo.sample_integer_option", 42,
                              "Sample integer option.")
    config_obj.DEFINE_string("SectionBar.sample_string_option", "",
                             "Sample string option.")
    config_obj.DEFINE_list("SectionBar.sample_list_option", [],
                           "Sample list option.")
    # This has to be defined as http_api.HttpRequestHandler.HandleRequest
    # depends on it and regression data won't get rendered without
    # this config option defined.
    config_obj.DEFINE_string("AdminUI.debug_impersonate_user", None, "")

    config = """
SectionFoo.sample_boolean_option: True
SectionBar.sample_string_option: "%(sAmPlE|lower)"
"""

    config_lib.LoadConfig(
        config_obj,
        config_fd=StringIO.StringIO(config),
        parser=config_lib.YamlParser)

    with utils.Stubber(config_lib, "CONFIG", config_obj):
      self.Check("GET", "/api/config")


class ApiGetConfigOptionHandlerTest(test_lib.GRRBaseTest):
  """Test for ApiGetConfigOptionHandler."""

  def setUp(self):
    super(ApiGetConfigOptionHandlerTest, self).setUp()
    self.handler = config_plugin.ApiGetConfigOptionHandler()

  def _ConfigStub(self, sections=None):
    mock = GetConfigMockClass(sections)
    config = config_lib.CONFIG
    return utils.MultiStubber((config, "GetRaw", mock["GetRaw"]),
                              (config, "Get", mock["Get"]),
                              (config, "type_infos", mock["type_infos"]))

  def _HandleConfigOption(self, stub_sections, name):
    with self._ConfigStub(stub_sections):
      result = self.handler.Handle(
          config_plugin.ApiGetConfigOptionArgs(name=name))

    return result

  def testRendersRedacted(self):
    input_dict = {
        "Mysql": {
            "database_password": {
                "value": u"secret",
                "raw_value": u"secret"
            }
        }
    }
    result = self._HandleConfigOption(input_dict, "Mysql.database_password")
    self.assertEqual(result.name, "Mysql.database_password")
    self.assertTrue(result.is_redacted)


class ApiGetConfigOptionHandlerRegressionTest(
    api_test_lib.ApiCallHandlerRegressionTest):

  api_method = "GetConfigOption"
  handler = config_plugin.ApiGetConfigOptionHandler

  def Run(self):
    config_obj = config_lib.GrrConfigManager()
    config_obj.DEFINE_string("SectionFoo.sample_string_option", "",
                             "Sample string option.")
    config_obj.DEFINE_string("Mysql.database_password", "", "Secret password.")
    # This has to be defined as http_api.HttpRequestHandler.HandleRequest
    # depends on it and regression data won't get rendered without
    # this config option defined.
    config_obj.DEFINE_string("AdminUI.debug_impersonate_user", None, "")

    config = """
SectionBar.sample_string_option: "%(sAmPlE|lower)"
Mysql.database_password: "THIS IS SECRET AND SHOULD NOT BE SEEN"
"""

    config_lib.LoadConfig(
        config_obj,
        config_fd=StringIO.StringIO(config),
        parser=config_lib.YamlParser)

    with utils.Stubber(config_lib, "CONFIG", config_obj):
      self.Check("GET", "/api/config/SectionFoo.sample_string_option")
      self.Check("GET", "/api/config/Mysql.database_password")
      self.Check("GET", "/api/config/NonExistingOption")


class ApiGrrBinaryTestMixin(object):
  """Mixing providing GRR binaries test setup routine."""

  def SetUpBinaries(self):
    with test_lib.FakeTime(42):
      code = "I am a binary file"
      upload_path = config_lib.CONFIG.Get("Config.aff4_root").Add(
          "executables/windows/test.exe")
      maintenance_utils.UploadSignedConfigBlob(
          code, aff4_path=upload_path, token=self.token)

    with test_lib.FakeTime(43):
      code = "I'm a python hack"
      upload_path = config_lib.CONFIG.Get("Config.python_hack_root").Add("test")
      maintenance_utils.UploadSignedConfigBlob(
          code, aff4_path=upload_path, token=self.token)

    with test_lib.FakeTime(44):
      component = test_lib.WriteComponent(
          name="grr-awesome-component",
          build_system=rdf_client.Uname(
              architecture="64bit",
              fqdn="test.host",
              kernel="3.42-generic",
              machine="x86_64",
              node="test.localhost",
              pep425tag="Linux_debian_64bit",
              release="Debian",
              system="Linux",
              version="4.42"),
          version="1.2.3.4",
          raw_data="I'm a component",
          modules=["grr_awesome"],
          token=self.token)
      return component.summary

  def GetBinaryBlob(self, summary):
    blob_urn = config_lib.CONFIG.Get("Client.component_aff4_stem").Add(
        summary.seed).Add("Linux_debian_64bit")
    return aff4.FACTORY.Open(blob_urn, token=self.token)


class ApiListGrrBinariesHandlerRegressionTest(
    ApiGrrBinaryTestMixin, api_test_lib.ApiCallHandlerRegressionTest):

  api_method = "ListGrrBinaries"
  handler = config_plugin.ApiListGrrBinariesHandler

  def Run(self):
    summary = self.SetUpBinaries()
    blob_fd = self.GetBinaryBlob(summary)

    self.Check(
        "GET",
        "/api/config/binaries",
        # Size of the ciphered blob depends on a number of factors,
        # including the random number generator. To avoid test flakiness,
        # it's better to substitute it with a predefined number.
        replace={summary.seed: "abcdef",
                 utils.SmartStr(blob_fd.size): "42"})


class ApiGetGrrBinaryHandlerRegressionTest(
    ApiGrrBinaryTestMixin, api_test_lib.ApiCallHandlerRegressionTest):

  api_method = "GetGrrBinary"
  handler = config_plugin.ApiGetGrrBinaryHandler

  def Run(self):
    summary = self.SetUpBinaries()

    self.Check("GET", "/api/config/binaries/python_hack/test")
    self.Check("GET", "/api/config/binaries/executable/windows/test.exe")

    blob_fd = self.GetBinaryBlob(summary)
    blob_contents = summary.cipher.Decrypt(blob_fd.Read(blob_fd.size))

    self.Check(
        "GET",
        "/api/config/binaries/component/"
        "grr-awesome-component_1.2.3.4/%s/Linux_debian_64bit" % summary.seed,
        # Serialized component contains a random seed, so we need to
        # replace it with a predefined string.
        replace={
            summary.seed: "abcdef",
            utils.SmartUnicode(blob_contents): "<serialized component>"
        })


def main(argv):
  test_lib.main(argv)


if __name__ == "__main__":
  flags.StartMain(main)
