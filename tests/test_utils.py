"""Discovery is offline and includes the installed switch and LED strip."""

from custom_components.cozylife import utils


async def test_offline_catalog_is_cached_and_has_known_models(mocker):
    mocker.patch.object(utils, "_CACHE_PID", [])
    bundled = mocker.patch.object(utils, "_get_bundled_pid_list", return_value=[])
    first = await utils.get_pid_list()
    assert first is await utils.get_pid_list()
    bundled.assert_called_once()
    models = {
        model["device_product_id"]
        for category in first
        for model in category["device_model"]
    }
    assert {"e5aHVS", "o0mmpn"} <= models
