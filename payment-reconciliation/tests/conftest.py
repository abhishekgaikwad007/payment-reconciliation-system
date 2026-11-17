import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from data_generator import generate_datasets
from reconciliation_engine import reconcile


@pytest.fixture(scope="session")
def dataset():
    return generate_datasets()


@pytest.fixture(scope="session")
def manifest(dataset):
    return dataset[2]


@pytest.fixture(scope="session")
def result(dataset):
    ledger, bank, _ = dataset
    return reconcile(ledger, bank)
