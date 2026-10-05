import ast
from pathlib import Path
import pytest

def enrich_volume(level):
    source = (Path(__file__).resolve().parents[1] / 'flask_app/server.py').read_text(encoding='utf-8')
    start = source.index('def enrich_snapshot(')
    end = source.index('    lower_level_raw =', start)
    prefix = source[start:end] + '    return data\n'
    tree = ast.parse(source)
    safe_float = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'safe_float')
    namespace = dict(parse_timestamp=lambda _: None, TANK_CAPACITY_LITERS=1000,
        load_device_simulator_state=lambda _: None,
        normalize_device_source=lambda value, **kwargs: value,
        DEVICE_SOURCE_REAL='real', get_device_source_mode=lambda: 'real',
        CONTROL_POLICY='firmware', resolve_device_type_label=lambda _: 'master')
    exec(compile(ast.Module(body=[safe_float], type_ignores=[]), '<safe_float>', 'exec'), namespace)
    exec(compile(prefix, '<enrich_snapshot_volume>', 'exec'), namespace)
    return namespace['enrich_snapshot']({'level': level, 'tank_height_cm': 100})

@pytest.mark.parametrize('level', [None, '', 'null', -1, 101, float('nan'), float('inf')])
def test_unknown_reading_is_not_an_empty_tank(level):
    result = enrich_volume(level)
    assert result['level'] is None
    assert result['remaining_liters'] is None
    assert result['water_available_label'] == '--'
    assert result['water_depth_cm'] is None

def test_real_zero_reading_remains_empty_tank():
    result = enrich_volume(0)
    assert result['level'] == 0
    assert result['remaining_liters'] == 0
    assert result['water_available_label'] == '0.0 L / 1000.0 L'
