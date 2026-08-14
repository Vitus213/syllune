from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
import type4me_linux.modes as modes_module

from type4me_linux.modes import (
    BUILTIN_MODES,
    ModesError,
    ModesRepository,
    render_template,
)
from type4me_linux.paths import AppPaths


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config=tmp_path / "config",
        data=tmp_path / "data",
        cache=tmp_path / "cache",
        state=tmp_path / "state",
        runtime=None,
    )


def test_exact_builtin_modes_are_seeded_with_stable_ids_and_prompts(tmp_path: Path) -> None:
    repository = ModesRepository(_paths(tmp_path))

    assert [
        (mode.id, mode.name, mode.prompt, mode.processing_label, mode.sort_order)
        for mode in repository.list()
    ] == [
        ("quick", "快速输入", "", "处理中", 0),
        (
            "voice-polish",
            "语音润色",
            "在不改变原意、不编造事实的前提下，删除口头语并修正语病，"
            "输出简体中文；英文与代码原样保留，数字一律使用阿拉伯数字。"
            "只输出处理后的文本，不要解释。原文：{text}",
            "润色中",
            1,
        ),
        (
            "prompt-optimize",
            "提示词优化",
            "将以下需求改写为清晰、可执行的提示词。保留所有事实和约束；"
            "只输出提示词，不要解释。原文：{text}",
            "优化中",
            2,
        ),
        (
            "translate-en",
            "翻译为英文",
            "将以下文本准确翻译为自然英文。只输出译文，不要解释。原文：{text}",
            "翻译中",
            3,
        ),
    ]
    assert repository.list() == BUILTIN_MODES
    stored = json.loads(repository.path.read_text(encoding="utf-8"))
    assert all(
        set(item) == {"id", "name", "prompt", "processing_label", "builtin", "sort_order"}
        for item in stored
    )
    assert [item["id"] for item in stored] == [
        "quick",
        "voice-polish",
        "prompt-optimize",
        "translate-en",
    ]


def test_uuid_user_mode_crud_is_atomic_and_resolves_id_name_and_default(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    fixed_id = uuid.UUID("12345678-1234-5678-9234-567812345678")
    repository = ModesRepository(paths, uuid_factory=lambda: fixed_id)

    created = repository.add(
        " 会议纪要 ",
        "整理{text}",
        "整理中",
        sort_order=15,
    )
    assert created.id == str(fixed_id)
    assert created.builtin is False
    assert repository.resolve(created.id) == created
    assert repository.resolve("会议纪要") == created
    assert repository.resolve("  会议纪要  ") == created
    assert repository.resolve(None).id == "quick"

    updated = repository.update(
        created.id,
        name="行动项",
        prompt="选择：{selected}；正文：{text}",
        processing_label="提取中",
        sort_order=35,
    )
    assert updated.name == "行动项"
    assert updated.sort_order == 35

    reloaded = ModesRepository(paths)
    assert reloaded.get(created.id) == updated
    assert reloaded.remove(created.id) == updated
    assert [mode.id for mode in reloaded.list()] == [mode.id for mode in BUILTIN_MODES]
    assert all(not path.name.endswith(".tmp") for path in paths.config.iterdir())


def test_failed_atomic_replace_preserves_file_and_memory_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = ModesRepository(_paths(tmp_path))
    before_file = repository.path.read_bytes()
    before_modes = repository.list()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("模拟磁盘故障")

    monkeypatch.setattr("type4me_linux.modes.os.replace", fail_replace)
    with pytest.raises(ModesError, match="无法原子写入"):
        repository.add("失败模式", "{text}")

    assert repository.path.read_bytes() == before_file
    assert repository.list() == before_modes
    assert not list(repository.path.parent.glob("*.tmp"))


def test_directory_fsync_failure_after_replace_keeps_committed_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_id = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    repository = ModesRepository(_paths(tmp_path), uuid_factory=lambda: fixed_id)
    real_fsync = modes_module.os.fsync
    calls = 0

    def fail_directory_fsync(file_descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("目录不支持 fsync")
        real_fsync(file_descriptor)

    monkeypatch.setattr(modes_module.os, "fsync", fail_directory_fsync)
    created = repository.add("已提交模式", "{text}")

    assert repository.get(created.id) == created
    assert ModesRepository(_paths(tmp_path)).get(created.id) == created


def test_builtins_are_immutable_and_name_conflicts_are_normalized(tmp_path: Path) -> None:
    repository = ModesRepository(
        _paths(tmp_path),
        uuid_factory=lambda: uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    )

    with pytest.raises(ModesError, match="内置模式不可修改"):
        repository.update("quick", name="别名")
    with pytest.raises(ModesError, match="内置模式不可删除"):
        repository.remove("translate-en")
    with pytest.raises(ModesError, match="模式名称已存在"):
        repository.add(" 快速输入 ", "{text}")


def test_render_template_is_single_pass_and_preserves_unknown_placeholders() -> None:
    rendered = render_template(
        "原文={text}|选中={selected}|剪贴板={clipboard}|未知={other}|再次={text}",
        text="正文含 {clipboard}",
        selected="选择含 {text}",
        clipboard="剪贴板含 {selected}",
    )

    assert rendered == (
        "原文=正文含 {clipboard}|选中=选择含 {text}|剪贴板=剪贴板含 {selected}"
        "|未知={other}|再次=正文含 {clipboard}"
    )
