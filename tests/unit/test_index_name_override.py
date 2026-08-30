"""A generation-per-index run needs the index name to be a parameter.

The name stays a constant and stays the default: the exported schema, the
drift gate and every existing caller must see byte-identical behaviour when
no name is passed. What this file pins is that passing one reaches all four
URL builders and the PUT body, because a name that reaches three of them
would send documents to one index and query another, with a 200 either way.
"""

from azgenai_lab.core.config import Settings
from azgenai_lab.models.search_index import INDEX_NAME, to_index_definition
from azgenai_lab.services.azure_search import AzureSearchClient, search_url
from azgenai_lab.services.search_data_plane import (
    SearchDataPlane,
    documents_url,
    index_url,
)

ENDPOINT = "https://example.search.windows.net"
OTHER = "azgenai-lab-chunks-g2"


def _settings() -> Settings:
    return Settings(
        azure_search_endpoint=ENDPOINT,
        azure_search_admin_key="k",
        use_fake_search=False,
        use_fake_embeddings=True,
    )


def test_url_builders_default_to_the_constant() -> None:
    assert INDEX_NAME in index_url(ENDPOINT)
    assert INDEX_NAME in documents_url(ENDPOINT)
    assert INDEX_NAME in search_url(ENDPOINT)


def test_url_builders_accept_an_override() -> None:
    assert f"/indexes/{OTHER}?" in index_url(ENDPOINT, OTHER)
    assert f"/indexes/{OTHER}/docs/index" in documents_url(ENDPOINT, OTHER)
    assert f"/indexes/{OTHER}/docs/search" in search_url(ENDPOINT, OTHER)


def test_put_body_name_matches_the_url_name() -> None:
    # Azure rejects a Create-or-Update Index whose body name disagrees with
    # the URL. Emitting the constant into a body PUT at another index is the
    # failure this asserts against.
    assert to_index_definition(OTHER)["name"] == OTHER


def test_definition_without_an_argument_is_unchanged() -> None:
    assert to_index_definition()["name"] == INDEX_NAME


def test_clients_carry_the_override() -> None:
    plane = SearchDataPlane(_settings(), index_name=OTHER)
    client = AzureSearchClient(_settings(), index_name=OTHER)
    assert f"/indexes/{OTHER}" in plane._index_url
    assert f"/indexes/{OTHER}" in client._url
