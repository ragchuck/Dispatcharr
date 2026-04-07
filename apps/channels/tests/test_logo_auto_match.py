"""Tests for logo auto-matching functionality.

Covers:
  - normalize_logo_name: extension stripping, separator normalisation, case folding
  - _find_best_logo_match: DB logo scoring, file scoring, threshold enforcement,
    registered-URL skipping, preference between DB and file matches
  - build_logo_candidates: DB query shape, directory scanning
  - ChannelViewSet.match_logos endpoint: bulk matching with/without channel_ids
  - ChannelViewSet.match_channel_logo endpoint: single-channel synchronous match
  - match_logo_channels task: all channels without a logo
  - match_selected_channels_logo task: selected channels without a logo
  - match_single_channel_logo task: single channel, skip if already has logo
"""
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.channels.models import Channel, ChannelGroup, Logo
from apps.channels.utils import (
    normalize_logo_name,
    build_logo_candidates,
    LOGO_MATCH_THRESHOLD,
)
from apps.channels.tasks import _find_best_logo_match

User = get_user_model()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class LogoMatchSetupMixin:
    """Shared helpers for building in-memory candidate lists."""

    def _db_logos(self, entries):
        return [
            {
                "id": i + 1,
                "name": name,
                "url": f"/data/logos/{name}.png",
                "norm_name": normalize_logo_name(name),
            }
            for i, name in enumerate(entries)
        ]

    def _file_candidates(self, filenames):
        return [
            (
                f"/data/logos/{fn}",
                fn.replace("-", " ").replace("_", " ").title(),
                normalize_logo_name(fn),
            )
            for fn in filenames
        ]


# ---------------------------------------------------------------------------
# normalize_logo_name
# ---------------------------------------------------------------------------

class NormalizeLogoNameTests(TestCase):
    """normalize_logo_name produces a consistent, comparable key from arbitrary names."""

    def test_lowercases_input(self):
        self.assertEqual(normalize_logo_name("ARD"), "ard")

    def test_strips_common_image_extension(self):
        self.assertEqual(normalize_logo_name("ard-de.png"), "ard de")

    def test_strips_svg_extension(self):
        self.assertEqual(normalize_logo_name("logo.svg"), "logo")

    def test_extension_longer_than_5_chars_not_stripped(self):
        result = normalize_logo_name("file.toolong")
        self.assertTrue(result.endswith("toolong"))

    def test_hyphens_become_spaces(self):
        self.assertEqual(normalize_logo_name("bbc-one"), "bbc one")

    def test_underscores_become_spaces(self):
        self.assertEqual(normalize_logo_name("bbc_two"), "bbc two")

    def test_collapses_internal_whitespace(self):
        self.assertEqual(normalize_logo_name("BBC   One"), "bbc one")

    def test_empty_string_returns_empty_string(self):
        self.assertEqual(normalize_logo_name(""), "")


# ---------------------------------------------------------------------------
# _find_best_logo_match
# ---------------------------------------------------------------------------

class FindBestLogoMatchExactTests(LogoMatchSetupMixin, TestCase):
    """High-confidence matches at or above the default threshold."""

    def test_exact_db_name_returns_db_id(self):
        db = self._db_logos(["ARD"])
        score, db_id, fp, fn = _find_best_logo_match("ard", db, [])
        self.assertEqual(db_id, 1)
        self.assertIsNone(fp)
        self.assertGreaterEqual(score, 90)

    def test_exact_file_name_returns_file_path(self):
        candidates = self._file_candidates(["ard-de.png"])
        score, db_id, fp, fn = _find_best_logo_match("ard de", [], candidates)
        self.assertIsNone(db_id)
        self.assertIsNotNone(fp)
        self.assertGreaterEqual(score, 90)

    def test_partial_name_match_scores_above_threshold(self):
        # "ARD Alpha" vs "ARD Alpha DE" — partial_ratio should push this over threshold.
        db = self._db_logos(["ARD Alpha DE"])
        score, db_id, fp, fn = _find_best_logo_match(
            normalize_logo_name("ARD Alpha"), db, []
        )
        self.assertIsNotNone(db_id)
        self.assertGreaterEqual(score, LOGO_MATCH_THRESHOLD)


class FindBestLogoMatchPreferenceTests(LogoMatchSetupMixin, TestCase):
    """When both DB and file candidates exist, the higher-scoring one wins."""

    def test_closer_db_logo_beats_weaker_file(self):
        db = self._db_logos(["BBC One"])
        candidates = self._file_candidates(["bbc-two-de.png"])
        score, db_id, fp, fn = _find_best_logo_match("bbc one", db, candidates)
        self.assertEqual(db_id, 1)
        self.assertIsNone(fp)

    def test_file_already_in_db_logos_is_skipped(self):
        """A file candidate whose path is already a registered DB logo URL must not be returned."""
        path = "/data/logos/ard-de.png"
        db = [{"id": 1, "name": "ARD DE", "url": path, "norm_name": "ard de"}]
        candidates = [(path, "Ard De", normalize_logo_name("ard-de.png"))]
        score, db_id, fp, fn = _find_best_logo_match("ard de", db, candidates)
        self.assertEqual(db_id, 1)
        self.assertIsNone(fp)


class FindBestLogoMatchThresholdTests(LogoMatchSetupMixin, TestCase):
    """Scores below threshold produce no match."""

    def test_unrelated_name_below_threshold_returns_no_match(self):
        db = self._db_logos(["Completely Unrelated Name XYZ"])
        score, db_id, fp, fn = _find_best_logo_match("ard", db, [], threshold=90)
        self.assertIsNone(db_id)
        self.assertIsNone(fp)

    def test_empty_candidate_lists_return_no_match(self):
        score, db_id, fp, fn = _find_best_logo_match("ard", [], [])
        self.assertIsNone(db_id)
        self.assertIsNone(fp)


# ---------------------------------------------------------------------------
# build_logo_candidates
# ---------------------------------------------------------------------------

class BuildLogoCandidatesTests(TestCase):
    """build_logo_candidates reflects the current DB and logo directory state."""

    def setUp(self):
        self.logo = Logo.objects.create(name="ARD", url="/data/logos/ard-de.png")

    def test_db_logo_is_included_in_db_logos(self):
        db_logos, _ = build_logo_candidates()
        self.assertIn(self.logo.id, [e["id"] for e in db_logos])

    def test_db_logo_norm_name_is_derived_from_name(self):
        db_logos, _ = build_logo_candidates()
        entry = next(e for e in db_logos if e["id"] == self.logo.id)
        self.assertEqual(entry["norm_name"], "ard")

    def test_db_logo_url_present_in_db_logos(self):
        db_logos, _ = build_logo_candidates()
        urls = {e["url"] for e in db_logos}
        self.assertIn("/data/logos/ard-de.png", urls)

    @patch("apps.channels.utils.os.path.isdir", return_value=True)
    @patch("apps.channels.utils.os.walk")
    def test_image_files_included_in_file_candidates(self, mock_walk, _mock_isdir):
        mock_walk.return_value = [("/data/logos", [], ["bbc-one-gb.png", "readme.txt"])]
        _, file_candidates = build_logo_candidates()
        paths = [fc[0] for fc in file_candidates]
        self.assertIn("/data/logos/bbc-one-gb.png", paths)

    @patch("apps.channels.utils.os.path.isdir", return_value=True)
    @patch("apps.channels.utils.os.walk")
    def test_non_image_files_excluded_from_file_candidates(self, mock_walk, _mock_isdir):
        mock_walk.return_value = [("/data/logos", [], ["bbc-one-gb.png", "readme.txt"])]
        _, file_candidates = build_logo_candidates()
        paths = [fc[0] for fc in file_candidates]
        self.assertFalse(any("readme" in p for p in paths))

    @patch("apps.channels.utils.os.path.isdir", return_value=True)
    @patch("apps.channels.utils.os.walk")
    def test_file_candidate_norm_name_derived_from_filename(self, mock_walk, _mock_isdir):
        mock_walk.return_value = [("/data/logos", [], ["arte-de.png"])]
        _, file_candidates = build_logo_candidates()
        self.assertEqual(file_candidates[0][2], "arte de")

    @patch("apps.channels.utils.os.path.isdir", return_value=False)
    def test_missing_logo_dir_yields_empty_file_candidates(self, _mock):
        _, file_candidates = build_logo_candidates()
        self.assertEqual(file_candidates, [])


# ---------------------------------------------------------------------------
# ChannelViewSet logo endpoints
# ---------------------------------------------------------------------------

class LogoMatchEndpointSetupMixin:
    def setUp(self):
        self.group = ChannelGroup.objects.create(name="Test Group")
        self.channel = Channel.objects.create(
            name="ARD", channel_number=1.0, channel_group=self.group
        )
        self.user = User.objects.create_user(username="testuser", password="pw")
        self.user.user_level = 10
        self.user.save()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)


class MatchLogosEndpointTests(LogoMatchEndpointSetupMixin, TestCase):
    """POST /api/channels/channels/match-logos/ — bulk logo matching."""

    url = "/api/channels/channels/match-logos/"

    def test_with_channel_ids_dispatches_selected_task(self):
        with patch("apps.channels.api_views.match_selected_channels_logo") as mock_task:
            mock_task.delay = MagicMock()
            response = self.client.post(self.url, {"channel_ids": [self.channel.id]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_task.delay.assert_called_once_with([self.channel.id])

    def test_without_channel_ids_dispatches_all_channels_task(self):
        with patch("apps.channels.api_views.match_logo_channels") as mock_task:
            mock_task.delay = MagicMock()
            response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_task.delay.assert_called_once_with()

    def test_response_contains_message(self):
        with patch("apps.channels.api_views.match_selected_channels_logo") as mock_task:
            mock_task.delay = MagicMock()
            response = self.client.post(self.url, {"channel_ids": [self.channel.id]}, format="json")
        self.assertIn("message", response.data)


class MatchChannelLogoEndpointTests(LogoMatchEndpointSetupMixin, TestCase):
    """POST /api/channels/channels/{id}/match-logo/ — single-channel logo match."""

    def _url(self):
        return f"/api/channels/channels/{self.channel.id}/match-logo/"

    def test_db_match_returns_matched_true_with_logo_and_score(self):
        logo = Logo.objects.create(name="ARD", url="https://example.com/ard.png")
        with patch("apps.channels.utils.os.path.isdir", return_value=False):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["matched"])
        self.assertEqual(response.data["logo"]["id"], logo.id)
        self.assertIn("score", response.data)

    def test_no_match_returns_matched_false(self):
        with patch("apps.channels.utils.os.path.isdir", return_value=False):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["matched"])

    def test_match_saves_logo_to_channel(self):
        logo = Logo.objects.create(name="ARD", url="https://example.com/ard.png")
        with patch("apps.channels.utils.os.path.isdir", return_value=False):
            self.client.post(self._url())
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.logo_id, logo.id)


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------

class LogoTaskSetupMixin:
    """Shared DB setup and task runner helpers."""

    def setUp(self):
        self.group = ChannelGroup.objects.create(name="Test Group")
        self.channel = Channel.objects.create(
            name="ARD", channel_number=1.0, channel_group=self.group
        )

    def _run_selected(self, channel_ids):
        from apps.channels.tasks import _match_logos_for_channels
        task = MagicMock()
        task.request.id = "test-task-id"
        channels = list(Channel.objects.filter(id__in=channel_ids, logo__isnull=True))
        with patch("core.utils.send_websocket_update"):
            return _match_logos_for_channels(task, channels)

    def _run_all(self):
        from apps.channels.tasks import _match_logos_for_channels
        from apps.channels.models import Channel
        task = MagicMock()
        task.request.id = "test-task-id"
        channels = list(Channel.objects.filter(logo__isnull=True))
        with patch("core.utils.send_websocket_update"):
            return _match_logos_for_channels(task, channels)

    def _run_single(self, channel_id):
        from apps.channels.tasks import _match_single_channel_logo
        with patch("core.utils.send_websocket_update"):
            return _match_single_channel_logo(channel_id)


class MatchSelectedChannelsLogoSkipTests(LogoTaskSetupMixin, TestCase):
    """Channels that already have a logo are excluded at the query level."""

    def test_channel_with_logo_is_not_processed(self):
        existing = Logo.objects.create(name="Some Logo", url="https://example.com/other.png")
        Logo.objects.create(name="ARD", url="https://example.com/ard.png")
        self.channel.logo = existing
        self.channel.save()
        with patch("apps.channels.utils.os.path.isdir", return_value=False):
            result = self._run_selected([self.channel.id])
        self.assertEqual(result["updated_count"], 0)
        self.channel.refresh_from_db()
        assert self.channel.logo is not None
        self.assertEqual(self.channel.logo.pk, existing.pk)

    @patch("apps.channels.utils.os.path.isdir", return_value=False)
    def test_channel_without_logo_is_matched(self, _mock):
        logo = Logo.objects.create(name="ARD", url="https://example.com/ard.png")
        result = self._run_selected([self.channel.id])
        self.assertEqual(result["updated_count"], 1)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.logo_id, logo.id)


class MatchSelectedChannelsLogoNoMatchTests(LogoTaskSetupMixin, TestCase):

    @patch("apps.channels.utils.os.path.isdir", return_value=False)
    def test_no_candidates_leaves_channel_unchanged(self, _mock):
        result = self._run_selected([self.channel.id])
        self.assertEqual(result["updated_count"], 0)
        self.channel.refresh_from_db()
        self.assertIsNone(self.channel.logo_id)


class MatchSelectedChannelsLogoFileMatchTests(LogoTaskSetupMixin, TestCase):

    @patch("apps.channels.utils.os.path.isdir", return_value=True)
    @patch("apps.channels.utils.os.walk")
    def test_file_match_creates_logo_and_assigns_it(self, mock_walk, _mock_isdir):
        mock_walk.return_value = [("/data/logos", [], ["ard-de.png"])]
        result = self._run_selected([self.channel.id])
        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(result["created_logos_count"], 1)
        self.channel.refresh_from_db()
        new_logo = Logo.objects.get(url="/data/logos/ard-de.png")
        self.assertEqual(self.channel.logo_id, new_logo.id)

    @patch("apps.channels.utils.os.path.isdir", return_value=True)
    @patch("apps.channels.utils.os.walk")
    def test_same_file_matched_by_two_channels_creates_one_logo(self, mock_walk, _mock_isdir):
        ch2 = Channel.objects.create(
            name="ARD HD", channel_number=2.0, channel_group=self.group
        )
        mock_walk.return_value = [("/data/logos", [], ["ard-de.png"])]
        result = self._run_selected([self.channel.id, ch2.id])
        self.assertEqual(result["created_logos_count"], 1)
        self.assertEqual(Logo.objects.filter(url="/data/logos/ard-de.png").count(), 1)


class MatchSelectedChannelsLogoMultiTests(LogoTaskSetupMixin, TestCase):

    @patch("apps.channels.utils.os.path.isdir", return_value=False)
    def test_multiple_channels_each_matched_independently(self, _mock):
        ch2 = Channel.objects.create(
            name="ZDF", channel_number=2.0, channel_group=self.group
        )
        Logo.objects.create(name="ARD", url="https://example.com/ard.png")
        Logo.objects.create(name="ZDF", url="https://example.com/zdf.png")
        result = self._run_selected([self.channel.id, ch2.id])
        self.assertEqual(result["updated_count"], 2)


class MatchSelectedChannelsLogoResultStructureTests(LogoTaskSetupMixin, TestCase):

    @patch("apps.channels.utils.os.path.isdir", return_value=False)
    def test_result_contains_required_keys(self, _mock):
        result = self._run_selected([self.channel.id])
        for key in ("status", "updated_count", "created_logos_count", "error_count", "errors"):
            self.assertIn(key, result)

    @patch("apps.channels.utils.os.path.isdir", return_value=False)
    def test_result_status_is_completed(self, _mock):
        result = self._run_selected([self.channel.id])
        self.assertEqual(result["status"], "completed")


class MatchLogoChannelsTests(LogoTaskSetupMixin, TestCase):
    """match_logo_channels processes all channels that have no logo."""

    @patch("apps.channels.utils.os.path.isdir", return_value=False)
    def test_channel_without_logo_is_processed(self, _mock):
        Logo.objects.create(name="ARD", url="https://example.com/ard.png")
        result = self._run_all()
        self.assertEqual(result["updated_count"], 1)

    def test_channel_with_logo_is_excluded(self):
        logo = Logo.objects.create(name="ARD", url="https://example.com/ard.png")
        self.channel.logo = logo
        self.channel.save()
        with patch("apps.channels.utils.os.path.isdir", return_value=False):
            result = self._run_all()
        self.assertEqual(result["updated_count"], 0)


class MatchSingleChannelLogoTests(LogoTaskSetupMixin, TestCase):
    """match_single_channel_logo handles all per-channel cases."""

    def test_channel_not_found_returns_matched_false(self):
        result = self._run_single(channel_id=99999)
        self.assertFalse(result["matched"])

    def test_channel_with_existing_logo_is_skipped(self):
        logo = Logo.objects.create(name="ARD", url="https://example.com/ard.png")
        self.channel.logo = logo
        self.channel.save()
        with patch("apps.channels.utils.os.path.isdir", return_value=False):
            result = self._run_single(self.channel.id)
        self.assertFalse(result["matched"])

    @patch("apps.channels.utils.os.path.isdir", return_value=False)
    def test_no_match_returns_matched_false(self, _mock):
        result = self._run_single(self.channel.id)
        self.assertFalse(result["matched"])

    @patch("apps.channels.utils.os.path.isdir", return_value=False)
    def test_db_match_assigns_logo_and_returns_matched_true(self, _mock):
        logo = Logo.objects.create(name="ARD", url="https://example.com/ard.png")
        result = self._run_single(self.channel.id)
        self.assertTrue(result["matched"])
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.logo_id, logo.id)

    @patch("apps.channels.utils.os.path.isdir", return_value=True)
    @patch("apps.channels.utils.os.walk")
    def test_file_match_creates_logo_and_returns_matched_true(self, mock_walk, _mock_isdir):
        mock_walk.return_value = [("/data/logos", [], ["ard-de.png"])]
        result = self._run_single(self.channel.id)
        self.assertTrue(result["matched"])
        self.assertTrue(Logo.objects.filter(url="/data/logos/ard-de.png").exists())
        self.channel.refresh_from_db()
        self.assertIsNotNone(self.channel.logo_id)
