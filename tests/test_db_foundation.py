import pytest
from sqlalchemy import make_url, text

from tests.conftest import _lookup, assert_safe_test_url


def test_basic_database_operation(db_session):
    db_session.execute(text("CREATE TEMP TABLE scratch (id int, name text)"))
    db_session.execute(text("INSERT INTO scratch VALUES (1, 'flowjob')"))
    assert db_session.execute(text("SELECT name FROM scratch WHERE id = 1")).scalar_one() == "flowjob"


def test_test_database_is_not_the_dev_database(test_database_url):
    dev_url = _lookup("DATABASE_URL")
    if dev_url:
        assert make_url(test_database_url).database != make_url(dev_url).database


# The next two tests prove isolation: the first commits real data, the second
# (which runs afterwards in file order) must not see it.
def test_isolation_part_1_commit_inside_test(db_session):
    db_session.execute(text("CREATE TABLE isolation_probe (id int)"))
    db_session.execute(text("INSERT INTO isolation_probe VALUES (1)"))
    db_session.commit()  # only releases a SAVEPOINT; the outer transaction is rolled back later
    assert db_session.execute(text("SELECT count(*) FROM isolation_probe")).scalar_one() == 1


def test_isolation_part_2_previous_test_left_nothing_behind(db_session):
    exists = db_session.execute(text("SELECT to_regclass('public.isolation_probe')")).scalar_one()
    assert exists is None


def test_unsafe_test_urls_are_refused():
    dev = "postgresql+psycopg://u:p@localhost/flowjob_dev"
    with pytest.raises(pytest.UsageError):
        assert_safe_test_url(dev, dev)
    with pytest.raises(pytest.UsageError):
        assert_safe_test_url("postgresql+psycopg://u:p@localhost/flowjob_prod", None)
    assert_safe_test_url("postgresql+psycopg://u:p@localhost/flowjob_test", dev)
