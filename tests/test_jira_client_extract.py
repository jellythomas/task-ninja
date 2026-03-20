"""Tests for JiraClient._extract_file_paths and _extract_text_from_adf."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.jira_client import JiraClient, _extract_text_from_adf


class TestExtractTextFromAdf:
    """Tests for the module-level ADF text extractor."""

    def test_none_returns_empty(self):
        assert _extract_text_from_adf({}) == ""

    def test_plain_text_node(self):
        node = {"type": "text", "text": "hello world"}
        assert _extract_text_from_adf(node) == "hello world"

    def test_nested_content(self):
        node = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Update "},
                        {"type": "text", "text": "src/auth/login.py"},
                    ],
                }
            ],
        }
        result = _extract_text_from_adf(node)
        assert "Update" in result
        assert "src/auth/login.py" in result

    def test_list_of_nodes(self):
        nodes = [
            {"type": "text", "text": "foo"},
            {"type": "text", "text": "bar"},
        ]
        assert _extract_text_from_adf(nodes) == "foo bar"

    def test_non_dict_non_list_returns_str(self):
        assert _extract_text_from_adf("plain") == "plain"


class TestExtractFilePaths:
    """Tests for JiraClient._extract_file_paths."""

    def test_none_description_returns_empty(self):
        assert JiraClient._extract_file_paths(None) == []

    def test_empty_string_returns_empty(self):
        assert JiraClient._extract_file_paths("") == []

    def test_plain_text_with_src_path(self):
        desc = "Please update src/auth/login.py and src/auth/utils.py"
        result = JiraClient._extract_file_paths(desc)
        assert "src/auth/login.py" in result
        assert "src/auth/utils.py" in result

    def test_deduplicated_paths(self):
        desc = "Update src/auth/login.py. Also see src/auth/login.py again."
        result = JiraClient._extract_file_paths(desc)
        assert result.count("src/auth/login.py") == 1

    def test_various_root_dirs(self):
        desc = "Touch api/routes.py, engine/orchestrator.py, models/ticket.py, migrations/0006.py"
        result = JiraClient._extract_file_paths(desc)
        assert any("api/routes.py" in p for p in result)
        assert any("engine/orchestrator.py" in p for p in result)
        assert any("models/ticket.py" in p for p in result)
        assert any("migrations/0006.py" in p for p in result)

    def test_caps_at_20(self):
        paths = " ".join(f"src/module{i}/file.py" for i in range(30))
        result = JiraClient._extract_file_paths(paths)
        assert len(result) <= 20

    def test_adf_dict_description(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Modify engine/jira_client.py for auth changes"}
                    ],
                }
            ],
        }
        result = JiraClient._extract_file_paths(adf)
        assert any("engine/jira_client.py" in p for p in result)

    def test_no_matching_paths_returns_empty(self):
        desc = "This ticket is about UI design and color changes."
        result = JiraClient._extract_file_paths(desc)
        assert result == []

    def test_preserves_order(self):
        desc = "First src/alpha.py then src/beta.py then src/gamma.py"
        result = JiraClient._extract_file_paths(desc)
        # Order should be alpha, beta, gamma
        assert result == ["src/alpha.py", "src/beta.py", "src/gamma.py"]
