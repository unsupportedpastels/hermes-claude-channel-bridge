"""Bridge-only vision declarations for hosts without profile-aware routing."""
from claude_native_bridge.api_service import _vision_config_updates
from claude_native_bridge.models import MODELS


def test_declare_every_bridge_model_without_changing_defaults():
    source = {"model": {"provider": "other", "default": "keep"}}
    update = _vision_config_updates(source)
    assert set(update) == {"providers"}
    assert update["providers"] == {
        "claude-native-bridge": {
            "models": {model: {"supports_vision": True} for model in MODELS}
        }
    }
    assert source == {"model": {"provider": "other", "default": "keep"}}


def test_preserve_explicit_per_model_vision_choices_and_other_metadata():
    source = {"providers": {"claude-native-bridge": {"models": {
        MODELS[0]: {"supports_vision": False, "context_length": 100000},
        MODELS[1]: {"vision": False},
    }}}}
    updates = _vision_config_updates(source)["providers"]["claude-native-bridge"]["models"]
    assert MODELS[0] not in updates and MODELS[1] not in updates
    assert source["providers"]["claude-native-bridge"]["models"][MODELS[0]]["context_length"] == 100000


def test_existing_declarations_need_no_update():
    config = _vision_config_updates({})
    assert _vision_config_updates(config) == {}


def test_declared_capabilities_route_user_images_natively_in_real_host():
    from agent.image_routing import decide_image_input_mode
    cfg = _vision_config_updates({})
    for model in MODELS:
        assert decide_image_input_mode("claude-native-bridge", model, cfg) == "native"
    cfg["agent"] = {"image_input_mode": "text"}
    assert decide_image_input_mode("claude-native-bridge", MODELS[0], cfg) == "text"
