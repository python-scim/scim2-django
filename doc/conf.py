import datetime
import os
import sys
from importlib import metadata

import django
from docutils import nodes

sys.path.insert(0, os.path.abspath(".."))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "examples.settings")
django.setup()

# -- General configuration ------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.doctest",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_design",
    "sphinx_issues",
    "sphinx_paramlinks",
]

templates_path = ["_templates"]
master_doc = "index"
project = "scim2-django"
year = datetime.datetime.now().strftime("%Y")
copyright = f"{year}, Yaal Coop"
author = "Yaal Coop"

version = metadata.version("scim2-django")
language = "en"
pygments_style = "sphinx"
toctree_collapse = False

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "django": (
        "https://docs.djangoproject.com/en/stable/",
        "https://docs.djangoproject.com/en/stable/_objects/",
    ),
    "scim2_server": ("https://scim2-server.readthedocs.io/en/latest/", None),
    "scim2_models": ("https://scim2-models.readthedocs.io/en/latest/", None),
    "scim2_client": ("https://scim2-client.readthedocs.io/en/latest/", None),
    "scim2_tester": ("https://scim2-tester.readthedocs.io/en/latest/", None),
    "scim2_cli": ("https://scim2-cli.readthedocs.io/en/latest/", None),
}

# -- Sibling projects ------------------------------------------------------

# Kept identical in every python-scim documentation, so that any divergence
# shows up in a diff.
NAV_LINKS = [
    {
        "title": "Libraries",
        "children": [
            {
                "title": "scim2-server",
                "url": "https://scim2-server.readthedocs.io",
                "summary": "Serve the SCIM protocol over any storage",
            },
            {
                "title": "scim2-client",
                "url": "https://scim2-client.readthedocs.io",
                "summary": "Pythonically build SCIM requests and parse SCIM responses",
            },
            {
                "title": "scim2-models",
                "url": "https://scim2-models.readthedocs.io",
                "summary": "SCIM resources and messages as Pydantic models",
            },
        ],
    },
    {
        "title": "Tools",
        "children": [
            {
                "title": "scim2-tester",
                "url": "https://scim2-tester.readthedocs.io",
                "summary": "Check a SCIM server for RFC compliance",
            },
            {
                "title": "scim2-cli",
                "url": "https://scim2-cli.readthedocs.io",
                "summary": "Query a SCIM server from the command line",
            },
            {
                "title": "pytest-scim2-server",
                "url": "https://github.com/pytest-dev/pytest-scim2-server",
                "summary": "A SCIM2 server fixture for pytest",
            },
        ],
    },
    {
        "title": "Integrations",
        "children": [
            {
                "title": "scim2-flask",
                "url": "https://scim2-flask.readthedocs.io",
                "summary": "Painless SCIM integration for Flask",
            },
            {
                "title": "scim2-django",
                "url": "https://scim2-django.readthedocs.io",
                "summary": "Painless SCIM integration for Django",
            },
            {
                "title": "scim2-fastapi",
                "url": "https://scim2-fastapi.readthedocs.io",
                "summary": "Painless SCIM integration for FastAPI",
            },
            {
                "title": "scim2-sqlalchemy",
                "url": "https://scim2-sqlalchemy.readthedocs.io",
                "summary": "Painless SCIM integration for SQLAlchemy",
            },
        ],
    },
]

# -- Options for HTML output ----------------------------------------------

html_theme = "shibuya"
html_baseurl = "https://scim2-django.readthedocs.io"
html_logo = "_static/python-scim.svg"
html_theme_options = {
    "globaltoc_expand_depth": 3,
    "accent_color": "grass",
    "github_url": "https://github.com/python-scim/scim2-django",
    "mastodon_url": "https://toot.aquilenet.fr/@yaal",
    "nav_links": NAV_LINKS,
}
html_context = {
    "source_type": "github",
    "source_user": "python-scim",
    "source_repo": "scim2-django",
    "source_version": "main",
    "source_docs_path": "/doc/",
}

# -- Options for sphinx-issues -------------------------------------

issues_github_path = "python-scim/scim2-django"


# -- Roles ----------------------------------------------------------------

MDN_HEADERS_URL = "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/"


def mdn_role(name, rawtext, text, lineno, inliner, options=None, content=None):
    """Link an HTTP header to its MDN page, and render its name as code."""
    reference = nodes.reference(
        rawtext, "", nodes.literal(text, text), refuri=MDN_HEADERS_URL + text
    )
    return [reference], []


def setup(app):
    app.add_role("mdn", mdn_role)
    app.add_crossref_type(
        directivename="setting",
        rolename="setting",
        indextemplate="pair: %s; setting",
    )
    app.add_crossref_type(
        directivename="django-admin",
        rolename="djadmin",
        indextemplate="pair: %s; django-admin command",
    )
