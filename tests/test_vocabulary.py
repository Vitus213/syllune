from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import type4me_linux.vocabulary as vocabulary_module
from type4me_linux.paths import AppPaths
from type4me_linux.vocabulary import VocabularyError, VocabularyService


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config=tmp_path / "config",
        data=tmp_path / "data",
        cache=tmp_path / "cache",
        state=tmp_path / "state",
        runtime=None,
    )


def _defaults(
    tmp_path: Path,
    *,
    hotwords: list[str] | None = None,
    snippets: dict[str, str] | None = None,
) -> Path:
    directory = tmp_path / "defaults"
    directory.mkdir()
    (directory / "hotwords.json").write_text(
        json.dumps(hotwords or [], ensure_ascii=False), encoding="utf-8"
    )
    (directory / "snippets.json").write_text(
        json.dumps(snippets or {}, ensure_ascii=False), encoding="utf-8"
    )
    return directory


def _write_users(
    paths: AppPaths,
    *,
    hotwords: list[str] | None = None,
    snippets: dict[str, str] | None = None,
) -> None:
    directory = paths.data / "vocabulary"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "hotwords.json").write_text(
        json.dumps(hotwords or [], ensure_ascii=False), encoding="utf-8"
    )
    (directory / "snippets.json").write_text(
        json.dumps(snippets or {}, ensure_ascii=False), encoding="utf-8"
    )


def test_merge_deduplicates_unicode_and_user_snippet_wins(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    defaults = _defaults(
        tmp_path,
        hotwords=["NixOS", "ＡＳＲ"],
        snippets={"My Email": "default@example.com", "项目名": "Type4Me"},
    )
    _write_users(
        paths,
        hotwords=["nixos", "asr", "Qwen"],
        snippets={"ｍｙemail": "user@example.com"},
    )

    service = VocabularyService(paths, defaults)

    assert service.list_hotwords() == ("NixOS", "ＡＳＲ", "Qwen")
    assert service.list_snippets() == {
        "ｍｙemail": "user@example.com",
        "项目名": "Type4Me",
    }
    assert paths.cache.joinpath("hotwords.txt").read_text(encoding="utf-8") == (
        "NixOS\nＡＳＲ\nQwen\n"
    )


def test_apply_snippets_allows_whitespace_case_and_ascii_boundaries(tmp_path: Path) -> None:
    service = VocabularyService(
        _paths(tmp_path),
        _defaults(
            tmp_path,
            snippets={"NixOS": "Linux", "我的邮箱": "me@example.com"},
        ),
    )

    result = service.apply_snippets("N i x O S, nixos! xNixOS NixOS2；我 的 邮 箱请保存 a我的邮箱b")

    assert result == "Linux, Linux! xNixOS NixOS2；me@example.com请保存 a我的邮箱b"


def test_atomic_crud_reload_and_cache_regeneration(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    service = VocabularyService(paths, _defaults(tmp_path, hotwords=["Default"]))

    assert service.add_hotword(" Qwen ") == ("Default", "Qwen")
    assert service.update_hotword("qWEN", "SenseVoice") == ("Default", "SenseVoice")
    assert service.add_snippet("邮箱", "one@example.com") == {"邮箱": "one@example.com"}
    assert service.update_snippet("邮箱", "two@example.com", new_trigger="我的邮箱") == {
        "我的邮箱": "two@example.com"
    }

    returned = service.list_snippets()
    returned["我的邮箱"] = "被调用方修改"
    assert service.list_snippets()["我的邮箱"] == "two@example.com"

    assert service.remove_hotword("sensevoice") == ("Default",)
    assert service.remove_snippet("我 的 邮 箱") == {}
    assert paths.cache.joinpath("hotwords.txt").read_text(encoding="utf-8") == "Default\n"

    user_directory = paths.data / "vocabulary"
    assert json.loads(user_directory.joinpath("hotwords.json").read_text(encoding="utf-8")) == []
    assert json.loads(user_directory.joinpath("snippets.json").read_text(encoding="utf-8")) == {}
    assert not list(user_directory.glob("*.tmp"))

    user_directory.joinpath("hotwords.json").write_text('["Reloaded"]', encoding="utf-8")
    user_directory.joinpath("snippets.json").write_text('{"重新加载": "成功"}', encoding="utf-8")
    service.reload()
    assert service.list_hotwords() == ("Default", "Reloaded")
    assert service.list_snippets() == {"重新加载": "成功"}
    assert paths.cache.joinpath("hotwords.txt").read_text(encoding="utf-8") == (
        "Default\nReloaded\n"
    )


def test_failed_reload_preserves_last_valid_snapshot(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    service = VocabularyService(
        paths,
        _defaults(tmp_path, hotwords=["Stable"], snippets={"触发": "结果"}),
    )
    hotwords_path = paths.data / "vocabulary" / "hotwords.json"
    hotwords_path.write_text("{", encoding="utf-8")

    with pytest.raises(VocabularyError, match="无法读取词汇文件"):
        service.reload()

    assert service.list_hotwords() == ("Stable",)
    assert service.list_snippets() == {"触发": "结果"}
    assert paths.cache.joinpath("hotwords.txt").read_text(encoding="utf-8") == "Stable\n"


def test_source_tree_packaged_defaults_are_discovered(tmp_path: Path) -> None:
    service = VocabularyService(_paths(tmp_path))

    assert service.list_hotwords() == ()
    assert service.list_snippets() == {}
    assert service.hotwords_cache_path.read_text(encoding="utf-8") == ""


def test_user_crud_reports_duplicates_conflicts_and_missing_entries(tmp_path: Path) -> None:
    service = VocabularyService(_paths(tmp_path), _defaults(tmp_path))
    service.add_hotword("Qwen")
    service.add_hotword("SenseVoice")
    service.add_snippet("邮箱", "one@example.com")
    service.add_snippet("项目", "Type4Me")

    with pytest.raises(VocabularyError, match="用户热词已存在"):
        service.add_hotword(" qWEN ")
    with pytest.raises(VocabularyError, match="用户热词已存在"):
        service.update_hotword("SenseVoice", "Ｑｗｅｎ")
    with pytest.raises(VocabularyError, match="找不到用户热词"):
        service.update_hotword("missing", "new")
    with pytest.raises(VocabularyError, match="找不到用户热词"):
        service.remove_hotword("missing")

    with pytest.raises(VocabularyError, match="用户片段触发词已存在"):
        service.add_snippet("邮 箱", "duplicate@example.com")
    with pytest.raises(VocabularyError, match="用户片段触发词已存在"):
        service.update_snippet("项目", "冲突", new_trigger="邮 箱")
    with pytest.raises(VocabularyError, match="找不到用户片段触发词"):
        service.update_snippet("missing", "new")
    with pytest.raises(VocabularyError, match="找不到用户片段触发词"):
        service.remove_snippet("missing")

    assert service.list_hotwords() == ("Qwen", "SenseVoice")
    assert service.list_snippets() == {
        "邮箱": "one@example.com",
        "项目": "Type4Me",
    }


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (lambda service: service.add_hotword("  "), "热词不能为空"),
        (lambda service: service.add_hotword(3), "热词必须是字符串"),
        (lambda service: service.add_snippet("  ", "value"), "片段触发词不能为空"),
        (lambda service: service.add_snippet("trigger", 3), "片段替换文本必须是字符串"),
        (lambda service: service.update_snippet("x", "value", new_trigger="  "), "找不到用户片段"),
    ],
)
def test_crud_rejects_invalid_text_inputs(
    tmp_path: Path,
    operation,  # type: ignore[no-untyped-def]
    message: str,
) -> None:
    service = VocabularyService(_paths(tmp_path), _defaults(tmp_path))

    with pytest.raises(VocabularyError, match=message):
        operation(service)


@pytest.mark.parametrize(
    ("filename", "payload", "message"),
    [
        ("hotwords.json", "{}", "JSON 数组"),
        ("hotwords.json", "[3]", "热词必须是字符串"),
        ("hotwords.json", '["  "]', "热词不能为空"),
        ("snippets.json", "[]", "JSON 对象"),
        ("snippets.json", '{"触发": 3}', "片段替换文本必须是字符串"),
        (
            "snippets.json",
            '{"A B": "first", "ab": "second"}',
            "规范化后重复",
        ),
    ],
)
def test_default_files_are_strictly_validated(
    tmp_path: Path,
    filename: str,
    payload: str,
    message: str,
) -> None:
    defaults = _defaults(tmp_path)
    defaults.joinpath(filename).write_text(payload, encoding="utf-8")

    with pytest.raises(VocabularyError, match=message):
        VocabularyService(_paths(tmp_path), defaults)


def test_both_packaged_default_files_are_required(tmp_path: Path) -> None:
    defaults = _defaults(tmp_path)
    defaults.joinpath("snippets.json").unlink()

    with pytest.raises(VocabularyError, match="找不到词汇文件"):
        VocabularyService(_paths(tmp_path), defaults)


def test_duplicate_of_default_hotword_remains_single_effective_entry(tmp_path: Path) -> None:
    service = VocabularyService(
        _paths(tmp_path),
        _defaults(tmp_path, hotwords=["NixOS"]),
    )

    assert service.add_hotword("nixos") == ("NixOS",)
    assert service.remove_hotword("NIXOS") == ("NixOS",)


def test_default_discovery_prefers_absolute_executable_share_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = tmp_path / "share" / "type4me-linux" / "vocabulary"
    defaults.mkdir(parents=True)
    defaults.joinpath("hotwords.json").write_text('["ExecutableDefault"]', encoding="utf-8")
    defaults.joinpath("snippets.json").write_text("{}", encoding="utf-8")
    executable = tmp_path / "bin" / "type4me-linux"
    executable.parent.mkdir()
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(executable)])

    service = VocabularyService(_paths(tmp_path))

    assert service.list_hotwords() == ("ExecutableDefault",)


def test_default_discovery_reports_when_no_candidate_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["relative-command"])
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "prefix"))
    monkeypatch.setattr(
        vocabulary_module,
        "__file__",
        str(tmp_path / "isolated" / "package" / "vocabulary.py"),
    )

    with pytest.raises(VocabularyError, match="找不到随应用安装的默认词汇目录"):
        VocabularyService(_paths(tmp_path))


def test_atomic_update_failure_preserves_last_loaded_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    service = VocabularyService(paths, _defaults(tmp_path, hotwords=["Stable"]))

    def fail_replace(source: Path, _destination: Path) -> None:
        source.unlink()
        raise OSError("只读文件系统")

    monkeypatch.setattr(vocabulary_module.os, "replace", fail_replace)

    with pytest.raises(VocabularyError, match="无法原子写入词汇文件"):
        service.add_hotword("New")

    assert service.list_hotwords() == ("Stable",)
    assert not list((paths.data / "vocabulary").glob("*.tmp"))
