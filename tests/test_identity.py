import unittest

from pilot107.core.identity import (
    IdentityMode,
    IdentityResolutionError,
    UserIdentity,
    is_safe_username,
    resolve_trusted_header_identity,
)


class IdentityTests(unittest.TestCase):
    def test_resolve_trusted_header_identity(self) -> None:
        resolution = resolve_trusted_header_identity(
            {"x-pilot107-user": " alice "},
            header_name="X-Pilot107-User",
            required=True,
        )

        self.assertEqual(
            resolution.identity,
            UserIdentity(username="alice", mode=IdentityMode.TRUSTED_HEADER),
        )
        self.assertIsNone(resolution.error)

    def test_required_identity_reports_missing(self) -> None:
        resolution = resolve_trusted_header_identity(
            {},
            header_name="X-Pilot107-User",
            required=True,
        )

        self.assertIsNone(resolution.identity)
        self.assertEqual(resolution.error, IdentityResolutionError.MISSING)

    def test_optional_identity_allows_missing(self) -> None:
        resolution = resolve_trusted_header_identity(
            {},
            header_name="X-Pilot107-User",
            required=False,
        )

        self.assertIsNone(resolution.identity)
        self.assertIsNone(resolution.error)

    def test_unsafe_identity_reports_forbidden(self) -> None:
        resolution = resolve_trusted_header_identity(
            {"X-Pilot107-User": "../alice"},
            header_name="X-Pilot107-User",
            required=True,
        )

        self.assertIsNone(resolution.identity)
        self.assertEqual(resolution.error, IdentityResolutionError.FORBIDDEN)

    def test_username_safety_contract(self) -> None:
        self.assertTrue(is_safe_username("alice_01.prod"))
        self.assertFalse(is_safe_username(""))
        self.assertFalse(is_safe_username("alice/bob"))
        self.assertFalse(is_safe_username("alice bob"))


if __name__ == "__main__":
    unittest.main()
