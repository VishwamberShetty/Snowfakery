from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from io import StringIO
from contextlib import contextmanager

from sqlalchemy import create_engine

import pytest

from snowfakery.cli import generate_cli
from snowfakery.data_generator import generate
from snowfakery.data_gen_exceptions import DataGenError
from snowfakery import generate_data

try:
    import cumulusci
except ImportError:
    cumulusci = False

sample_mapping_yaml = Path(__file__).parent / "mapping_vanilla_sf.yml"
sample_accounts_yaml = Path(__file__).parent / "gen_sf_standard_objects.yml"

sample_yaml = Path(__file__).parent / "include_parent.yml"


skip_if_cumulusci_missing = pytest.mark.skipif(
    not hasattr(cumulusci, "api"), reason="CumulusCI not installed"
)


class Test_CLI_CCI:
    # @skip_if_cumulusci_missing
    def test_mapping_file(self):
        with TemporaryDirectory() as t:
            url = f"sqlite:///{t}/foo.db"
            generate_cli.main(
                [
                    str(sample_accounts_yaml),
                    "--dburl",
                    url,
                ],
                standalone_mode=False,
            )

            engine = create_engine(url)
            with engine.connect() as connection:
                result = list(connection.execute("select * from Account"))
                assert result[0]["id"] == 1
                assert result[0]["BillingCountry"] == "Canada"


class FakeSimpleSalesforce:
    def __init__(self, query_responses):
        self.query_responses = query_responses

    def query(self, query: str):
        try:
            return self.query_responses[query]
        except KeyError:
            raise KeyError(f"No mock response found for Salesforce query `{query}`")


fake_sf_client = FakeSimpleSalesforce(
    {
        "SELECT count() FROM Account": {"totalSize": 10},
        "SELECT Id FROM Account LIMIT 1": {"records": [{"Id": "FAKEID0"}]},
        "SELECT Id FROM Account LIMIT 1 OFFSET 0": {"records": [{"Id": "FAKEID0"}]},
        "SELECT Id FROM Account LIMIT 1 OFFSET 5": {"records": [{"Id": "FAKEID5"}]},
    }
)


class TestSOQLNoCCI:
    @patch(
        "snowfakery.standard_plugins.Salesforce.SalesforceConnection.sf",
        wraps=fake_sf_client,
    )
    @patch("snowfakery.standard_plugins.Salesforce.randrange", lambda *arg, **kwargs: 5)
    def test_soql_plugin_random(self, fake_sf_client, generated_rows):
        yaml = """
            - plugin: snowfakery.standard_plugins.Salesforce.SalesforceQuery
            - object: Contact
              fields:
                FirstName: Suzy
                LastName: Salesforce
                AccountId:
                    SalesforceQuery.random_record: Account
        """
        generate(StringIO(yaml), plugin_options={"orgname": "blah"})
        assert fake_sf_client.mock_calls
        assert generated_rows.row_values(0, "AccountId") == "FAKEID5"

    @patch(
        "snowfakery.standard_plugins.Salesforce.SalesforceConnection.sf",
        wraps=fake_sf_client,
    )
    @patch("snowfakery.standard_plugins.Salesforce.randrange", lambda *arg, **kwargs: 5)
    def test_soql_plugin_record(self, fake_sf_client, generated_rows):
        yaml = """
            - plugin: snowfakery.standard_plugins.Salesforce.SalesforceQuery
            - object: Contact
              fields:
                FirstName: Suzy
                LastName: Salesforce
                AccountId:
                    SalesforceQuery.find_record: Account
        """
        generate(StringIO(yaml), plugin_options={"orgname": "blah"})
        assert fake_sf_client.mock_calls
        assert generated_rows.row_values(0, "AccountId") == "FAKEID0"


class TestSOQLWithCCI:
    @patch("snowfakery.standard_plugins.Salesforce.randrange", lambda *arg, **kwargs: 0)
    @pytest.mark.vcr()
    @skip_if_cumulusci_missing
    def test_soql(self, sf, org_config, generated_rows):
        yaml = """
            - plugin: snowfakery.standard_plugins.Salesforce.SalesforceQuery
            - object: Contact
              fields:
                FirstName: Suzy
                LastName: Salesforce
                AccountId:
                    SalesforceQuery.random_record: Account
            - object: Contact
              fields:
                FirstName: Sammy
                LastName: Salesforce
                AccountId:
                    SalesforceQuery.random_record: Account
        """
        assert org_config.name
        sf.Account.create({"Name": "Company"})
        generate(StringIO(yaml), plugin_options={"orgname": org_config.name})
        assert len(generated_rows.mock_calls) == 2

    # @skip_if_cumulusci_missing
    @pytest.mark.vcr()
    def test_missing_orgname(self, sf):
        yaml = """
            - plugin: snowfakery.standard_plugins.Salesforce.SalesforceQuery
            - object: Contact
              fields:
                AccountId:
                    SalesforceQuery.random_record: Account
        """
        with pytest.raises(DataGenError):
            generate(StringIO(yaml), {})

    @patch("snowfakery.standard_plugins.Salesforce.randrange", lambda *arg, **kwargs: 1)
    @pytest.mark.vcr()
    def test_example_through_api(self, sf, generated_rows, org_config):
        sf.Account.create({"Name": "Company3"})
        filename = (
            Path(__file__).parent.parent / "examples/salesforce_soql_example.recipe.yml"
        )
        generate_data(filename, plugin_options={"orgname": org_config.name})
        assert generated_rows.mock_calls

    def test_cci_not_available(self):
        filename = (
            Path(__file__).parent.parent / "examples/salesforce_soql_example.recipe.yml"
        )
        with unittest.mock.patch(
            "snowfakery.standard_plugins.Salesforce.SalesforceConnection._get_sf_clients"
        ) as conn:
            conn.side_effect = ImportError(
                "cumulusci module cannot be loaded by snowfakery"
            )
            with pytest.raises(Exception, match="cumulusci module cannot be loaded"):
                generate_data(filename, plugin_options={"orgname": "None"})


# TODO: add tests for SOQLDatasets
#       ensure that all documented params/methods are covered.
class TestSOQLDatasets:
    @pytest.mark.vcr()
    def test_soql_dataset_shuffled(self, sf, org_config, generated_rows):
        filename = (
            Path(__file__).parent.parent / "examples/soql_dataset_shuffled.recipe.yml"
        )

        generate_data(filename, plugin_options={"orgname": org_config.name})
        assert len(generated_rows.mock_calls) == 10

        for mock_call in generated_rows.mock_calls:
            row_type, row_data = mock_call[1]
            assert row_type == "Contact"
            assert row_data["OwnerId"].startswith("005")
            assert row_data["LastName"]

        # TODO: anon apex is better, so IDs don't end up in the VCR logs.
        sf.restful(
            "tooling/executeAnonymous",
            {
                "anonymousBody": "delete [SELECT Id FROM Contact WHERE Name LIKE 'TestUser%'];"
            },
        )

    @pytest.mark.vcr()
    def test_soql_dataset_in_order(self, sf, org_config, generated_rows):
        filename = Path(__file__).parent.parent / "examples/soql_dataset.recipe.yml"

        generate_data(filename, plugin_options={"orgname": org_config.name})
        assert len(generated_rows.mock_calls) == 10

        for mock_call in generated_rows.mock_calls:
            row_type, row_data = mock_call[1]
            assert row_type == "Contact"
            assert row_data["OwnerId"].startswith("005")
            assert row_data["LastName"]

        first_user_lastname = sf.query("select LastName from User")["records"][0][
            "LastName"
        ]
        assert generated_rows.mock_calls[0][1][1]["LastName"] == first_user_lastname

        # TODO: anon apex is better, so IDs don't end up in the VCR logs.
        sf.restful(
            "tooling/executeAnonymous",
            {
                "anonymousBody": "delete [SELECT Id FROM Contact WHERE Name LIKE 'TestUser%'];"
            },
        )

    @pytest.mark.vcr()
    def test_soql_dataset_where(self, sf, org_config, generated_rows):
        filename = (
            Path(__file__).parent.parent / "examples/soql_dataset_where.recipe.yml"
        )

        generate_data(filename, plugin_options={"orgname": org_config.name})
        assert len(generated_rows.mock_calls) == 10

        for mock_call in generated_rows.mock_calls:
            row_type, row_data = mock_call[1]
            assert row_type == "Contact"
            assert row_data["OwnerId"].startswith("005")
            assert row_data["FirstName"].startswith("A")

        # TODO: anon apex is better, so IDs don't end up in the VCR logs.
        sf.restful(
            "tooling/executeAnonymous",
            {
                "anonymousBody": "delete [SELECT Id FROM Contact WHERE Name LIKE 'TestUser%'];"
            },
        )

    @pytest.mark.vcr()
    def test_soql_dataset_bulk(self, sf, org_config, generated_rows):
        filename = (
            Path(__file__).parent.parent / "examples/soql_dataset_where.recipe.yml"
        )

        # pretend there are 5000 records in org
        pretend_5000 = patch(
            "simple_salesforce.api.Salesforce.restful",
            lambda *args, **kwargs: {"sObjects": [{"name": "User", "count": 5000}]},
        )
        csv_data = """"Id","FirstName","LastName"
"0051F00000nc59NQAQ","Automated","Process" """

        @contextmanager
        def download_file(*args, **kwargs):
            yield StringIO(csv_data)

        do_not_really_download = patch(
            "cumulusci.tasks.bulkdata.step.download_file",
            download_file,
        )
        with pretend_5000, do_not_really_download:
            generate_data(filename, plugin_options={"orgname": org_config.name})

        assert len(generated_rows.mock_calls) == 10

        for mock_call in generated_rows.mock_calls:
            row_type, row_data = mock_call[1]
            assert row_type == "Contact"
            assert row_data["OwnerId"].startswith("005")
            assert row_data["FirstName"].startswith("A")

        # TODO: anon apex is better, so IDs don't end up in the VCR logs.
        sf.restful(
            "tooling/executeAnonymous",
            {
                "anonymousBody": "delete [SELECT Id FROM Contact WHERE Name LIKE 'TestUser%'];"
            },
        )
