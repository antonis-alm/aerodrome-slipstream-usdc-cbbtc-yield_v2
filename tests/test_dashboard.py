import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


def _load_dashboard_module():
    sys.modules["streamlit"] = SimpleNamespace(title=lambda *_args, **_kwargs: None)

    almanak_module = ModuleType("almanak")
    framework_module = ModuleType("almanak.framework")
    dashboard_module = ModuleType("almanak.framework.dashboard")
    templates_module = ModuleType("almanak.framework.dashboard.templates")

    templates_module.get_aerodrome_config = lambda **_kwargs: None
    templates_module.prepare_lp_session_state = lambda *_args, **_kwargs: None
    templates_module.render_lp_dashboard = lambda *_args, **_kwargs: None

    almanak_module.framework = framework_module
    framework_module.dashboard = dashboard_module
    dashboard_module.templates = templates_module

    sys.modules["almanak"] = almanak_module
    sys.modules["almanak.framework"] = framework_module
    sys.modules["almanak.framework.dashboard"] = dashboard_module
    sys.modules["almanak.framework.dashboard.templates"] = templates_module

    sys.modules.pop("dashboard.ui", None)
    return importlib.import_module("dashboard.ui")


def test_dashboard_imports():
    module = _load_dashboard_module()
    assert callable(module.render_custom_dashboard)


def test_parse_pool_tokens():
    module = _load_dashboard_module()
    assert module._parse_pool_tokens("USDC/CBBTC/100") == ("USDC", "CBBTC")
    assert module._parse_pool_tokens("USDC/CBBTC") == ("USDC", "CBBTC")
    assert module._parse_pool_tokens("") == ("USDC", "CBBTC")


def test_render_custom_dashboard_uses_aerodrome_template():
    module = _load_dashboard_module()

    deployment_id = "dep-1"
    strategy_config = {
        "pool": "USDC/CBBTC/100",
        "pool_type": "volatile",
        "chain": "base",
        "atr_timeframe": "1h",
    }
    api_client = object()
    session_state = {"existing": "state"}

    with (
        patch.object(module.st, "title") as title,
        patch.object(module, "get_aerodrome_config", return_value="lp-config") as get_config,
        patch.object(module, "prepare_lp_session_state", return_value={"prepared": True}) as prepare,
        patch.object(module, "render_lp_dashboard") as render,
    ):
        module.render_custom_dashboard(deployment_id, strategy_config, api_client, session_state)

    title.assert_called_once_with("Aerodrome Slipstream USDC-CBBTC Yield")
    get_config.assert_called_once_with(
        token0="USDC",
        token1="CBBTC",
        pool_type="volatile",
        chain="base",
        timeframe="1h",
    )
    prepare.assert_called_once_with(api_client, session_state=session_state, config="lp-config")
    render.assert_called_once_with(
        deployment_id,
        strategy_config,
        {"prepared": True},
        "lp-config",
        api_client=api_client,
    )
