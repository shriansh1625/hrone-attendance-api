"""Tests use a unique disposable database; no existing database is touched."""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient


@pytest.fixture(scope="session")
def api_module():
    os.environ["MONGO_URI"] = os.getenv("TEST_MONGO_URI", "mongodb://127.0.0.1:27017")
    os.environ["MONGO_DB"] = "hrone_test_" + uuid.uuid4().hex
    from app import main

    main.db.command("ping")
    yield main
    # Only the randomly named database created by this fixture is disposable.
    cleanup = MongoClient(os.environ["MONGO_URI"])
    cleanup.drop_database(os.environ["MONGO_DB"])
    cleanup.close()


@pytest.fixture(scope="session")
def http(api_module):
    with TestClient(api_module.app) as client:
        yield client


@pytest.fixture(autouse=True)
def clean_collections(http, api_module):
    api_module.db.employees.delete_many({})
    api_module.db.attendance_logs.delete_many({})
