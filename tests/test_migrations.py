"""Guards on the migration graph itself.

A revision id that collides with an existing one, or a down_revision that
points anywhere but the current head, produces a second head — and alembic
refuses to run at all with "Multiple head revisions are present". Nothing
about that is visible in a normal test run, because the suite builds its
schema from the models rather than by migrating, so it only surfaces on
deploy, where the entrypoint runs `alembic upgrade head` and the container
fails to start.
"""
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def script_directory() -> ScriptDirectory:
    """Alembic's own view of the graph, not a re-implementation of it."""
    return ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))


def test_there_is_exactly_one_head(script_directory):
    heads = script_directory.get_heads()
    assert len(heads) == 1, (
        f"{len(heads)} heads: {heads}. `alembic upgrade head` cannot choose "
        "between them and the deploy fails. Set the new migration's "
        "down_revision to the previous head."
    )


def test_revision_ids_are_unique():
    """ScriptDirectory silently keeps one of a duplicated pair, so the files
    are read directly here."""
    import re
    from collections import Counter

    ids = []
    for path in (REPO_ROOT / "alembic" / "versions").glob("*.py"):
        match = re.search(
            r"^revision(?:\s*:\s*str)?\s*=\s*['\"]([^'\"]+)['\"]",
            path.read_text(), re.M,
        )
        assert match, f"{path.name} declares no revision id"
        ids.append(match.group(1))

    duplicates = [rev for rev, count in Counter(ids).items() if count > 1]
    assert not duplicates, (
        f"revision ids used more than once: {duplicates}. Alembic warns "
        "'present more than once' and the graph becomes ambiguous."
    )


def test_every_migration_is_reachable_from_base(script_directory):
    """A down_revision naming a revision that does not exist, or a chain that
    forks off the trunk, leaves migrations that upgrade head never runs."""
    head = script_directory.get_current_head()
    walked = {rev.revision for rev in script_directory.iterate_revisions(head, "base")}
    everything = {rev.revision for rev in script_directory.walk_revisions()}
    assert walked == everything, (
        f"not on the path from base to head: {sorted(everything - walked)}"
    )
