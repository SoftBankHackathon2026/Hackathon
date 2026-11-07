import importlib
import pathlib
import sys

import pytest

_MIGRATION_DIR = pathlib.Path(__file__).resolve().parents[2] / "migration"


@pytest.fixture
def migration_module():
    """원가연 migration 패키지의 모듈을 불러온다(계약 시험용, 읽기만)."""

    def load(name):
        sys.path.insert(0, str(_MIGRATION_DIR))
        try:
            return importlib.import_module(name)
        except ImportError:
            pytest.skip(f"원가연 {name} 을 불러올 수 없다")
        finally:
            sys.path.remove(str(_MIGRATION_DIR))

    return load
