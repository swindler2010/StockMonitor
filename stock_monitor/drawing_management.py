"""Atomic user annotation operations; no market, account or strategy writes."""
import copy
import json
import uuid

from drawing_actions import modify


def selection(objects, indices, *, groups=True):
    indices = set(indices)
    if not indices or any(type(i) is not int or not 0 <= i < len(objects) for i in indices):
        raise ValueError('Выберите объекты')
    if groups:
        ids = {objects[i].get('group_id') for i in indices} - {None, ''}
        indices.update(i for i, obj in enumerate(objects) if obj.get('group_id') in ids)
    return sorted(indices)


def batch(objects, indices, operation, value=None):
    indices = selection(objects, indices)
    # Validate the entire selection first: a locked member must never cause a
    # partial edit. Unlocking, visibility and duplication remain available.
    if operation not in ('visible', 'locked', 'copy') and any(objects[i].get('locked') for i in indices):
        raise ValueError('В группе есть закреплённый объект. Сначала снимите закрепление.')
    result = copy.deepcopy(objects)
    if operation == 'group':
        if len(indices) < 2:
            raise ValueError('Для группы нужны минимум два объекта')
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 40 or any(ord(c) < 32 for c in value):
            raise ValueError('Имя группы: от 1 до 40 символов')
        group = uuid.uuid4().hex
        for i in indices:
            result[i].update(group_id=group, group_name=value.strip())
    elif operation == 'ungroup':
        for i in indices:
            result[i].pop('group_id', None)
            result[i].pop('group_name', None)
    elif operation == 'delete':
        result = [obj for i, obj in enumerate(result) if i not in indices]
    elif operation == 'copy':
        groups = {}
        copies = []
        for i in indices:
            obj = copy.deepcopy(result[i])
            if obj.get('group_id'):
                obj['group_id'] = groups.setdefault(obj['group_id'], uuid.uuid4().hex)
            copies.append(obj)
        result.extend(copies)
    elif operation in ('front', 'back'):
        chosen = [obj for i, obj in enumerate(result) if i in indices]
        rest = [obj for i, obj in enumerate(result) if i not in indices]
        result = rest + chosen if operation == 'front' else chosen + rest
    else:
        for i in indices:
            result = modify(result, i, operation, value)
    for i, obj in enumerate(result):
        obj['stack_order'] = i
    return result


def export_document(info, identity, objects, indices=None):
    """JSON is data only. Export selected groups or all annotations, never the
    instrument dictionary (which can contain private positions and account data).
    """
    chosen = selection(objects, indices) if indices is not None else range(len(objects))
    drawings = [copy.deepcopy(objects[i]) for i in chosen]
    if len(drawings) > 5000:
        raise ValueError('Слишком много объектов для экспорта')
    payload = dict(format='StockMonitor.drawings', version=1,
                   instrument={k: info.get(k) for k in ('isin', 'symbol', 'venue', 'currency')},
                   chart=dict(zip(('source', 'venue', 'currency', 'symbol', 'timeframe', 'base_tf'), identity)),
                   drawings=drawings)
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    if len(text.encode('utf-8')) > 3_000_000:
        raise ValueError('Экспорт превышает 3 МБ')
    return text
