"""config.local.json 机器配置外置测试（§21）：覆盖/派生/容错。"""
import importlib
import json

import pytest

from server import config


@pytest.fixture()
def restore_config():
    """用例内对 config 模块的全局改动，用例后 reload 还原为仓库默认值。"""
    yield
    importlib.reload(config)


def _write_local(tmp_path, data):
    p = tmp_path / "config.local.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    config.CONFIG_LOCAL_PATH = p


def test_no_local_file_keeps_defaults(tmp_path, restore_config):
    config.CONFIG_LOCAL_PATH = tmp_path / "absent.json"
    before = (config.COMFYUI_DIR, config.LM_PROFILES, config.PI_SKILL_DIR)
    config._apply_local_overrides()
    assert (config.COMFYUI_DIR, config.LM_PROFILES, config.PI_SKILL_DIR) == before


def test_override_paths_and_profiles(tmp_path, restore_config):
    _write_local(tmp_path, {
        "pi_skill_dir": "E:/skills/pw",
        "lm_profiles": {
            "fast": {"cmd": "E:/llm/start.cmd", "alias": "m@fast", "label": "快"},
        },
        "comfyui_dir": "E:/ComfyUI/app",
        "web_port": 9200,
    })
    config._apply_local_overrides()
    assert str(config.PI_SKILL_DIR) == "E:\\skills\\pw"
    assert config.LM_PROFILES["fast"]["alias"] == "m@fast"
    # 档位表没有 iq2_m_64k → 默认档位与 alias 整体回退第一项
    assert config.LM_MODEL_ALIAS == "m@fast"
    assert config.DEFAULT_PARAMS["lm_profile"] == "fast"
    # comfyui_python / comfy_models_dir 派生自 comfyui_dir（整合包布局）
    assert config.COMFYUI_DIR == config.Path("E:/ComfyUI/app")
    assert config.COMFYUI_PYTHON == config.Path("E:/ComfyUI/python/python.exe")
    assert config.COMFY_MODELS_DIR == config.Path("E:/ComfyUI/app/models/diffusion_models")
    assert config.WEB_PORT == 9200


def test_explicit_overrides_win_over_derived(tmp_path, restore_config):
    _write_local(tmp_path, {
        "comfyui_dir": "E:/ComfyUI/app",
        "comfyui_python": "F:/py/python.exe",
        "comfy_models_dir": "G:/models",
    })
    config._apply_local_overrides()
    assert config.COMFYUI_PYTHON == config.Path("F:/py/python.exe")
    assert config.COMFY_MODELS_DIR == config.Path("G:/models")


def test_default_profile_key_kept_when_present(tmp_path, restore_config):
    _write_local(tmp_path, {
        "lm_profiles": {
            "iq2_m_64k": {"cmd": "E:/a.cmd", "alias": "m@a", "label": "A"},
            "q4_k_m": {"cmd": "E:/b.cmd", "alias": "m@b", "label": "B"},
        },
    })
    config._apply_local_overrides()
    assert config.LM_MODEL_ALIAS == "m@a"
    assert config.DEFAULT_PARAMS["lm_profile"] == "iq2_m_64k"


def test_bad_json_raises_runtime_error(tmp_path, restore_config):
    p = tmp_path / "config.local.json"
    p.write_text("{oops", encoding="utf-8")
    config.CONFIG_LOCAL_PATH = p
    with pytest.raises(RuntimeError, match="config.local.json 解析失败"):
        config._apply_local_overrides()
