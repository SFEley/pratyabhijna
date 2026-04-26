from pratyabhijna.tools.audit import AUDIT_REVISION


def test_audit_revision_is_int():
    assert isinstance(AUDIT_REVISION, int)
    assert AUDIT_REVISION >= 1
