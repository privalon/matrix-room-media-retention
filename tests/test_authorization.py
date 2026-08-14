from matrix_room_media_retention.authorization import is_authorized


class TestIsAuthorized:
    def test_explicit_moderator_authorized(self):
        content = {"users": {"@mod:example.org": 50}, "users_default": 0}
        assert is_authorized(power_levels_content=content, sender="@mod:example.org") is True

    def test_explicit_admin_authorized(self):
        content = {"users": {"@admin:example.org": 100}, "users_default": 0}
        assert is_authorized(power_levels_content=content, sender="@admin:example.org") is True

    def test_default_user_not_authorized(self):
        content = {"users": {}, "users_default": 0}
        assert is_authorized(power_levels_content=content, sender="@rando:example.org") is False

    def test_explicit_low_level_not_authorized(self):
        content = {"users": {"@low:example.org": 10}, "users_default": 0}
        assert is_authorized(power_levels_content=content, sender="@low:example.org") is False

    def test_users_default_above_threshold_authorizes_everyone(self):
        # An unusual but valid room config (e.g. a small trusted family
        # room where everyone defaults to moderator) -- users_default
        # itself must be honored, not just per-user overrides.
        content = {"users": {}, "users_default": 50}
        assert is_authorized(power_levels_content=content, sender="@anyone:example.org") is True

    def test_custom_minimum_level(self):
        content = {"users": {"@vip:example.org": 75}, "users_default": 0}
        assert is_authorized(power_levels_content=content, sender="@vip:example.org", minimum_level=75) is True
        assert is_authorized(power_levels_content=content, sender="@vip:example.org", minimum_level=76) is False

    def test_missing_users_key_falls_back_to_default(self):
        content = {"users_default": 0}
        assert is_authorized(power_levels_content=content, sender="@anyone:example.org") is False
