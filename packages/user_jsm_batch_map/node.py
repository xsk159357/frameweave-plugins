from __future__ import annotations
import json, os, re
from typing import Any, Dict
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType
from app import batchlib

_TPL = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")

def _get(obj, path):
    cur = obj
    for part in path.split('.'):
        if isinstance(cur, dict): cur = cur.get(part)
        else: return ''
    return '' if cur is None else cur

def _render(value, item):
    if isinstance(value, str): return _TPL.sub(lambda m: str(_get(item, m.group(1))), value)
    if isinstance(value, dict): return {k: _render(v, item) for k, v in value.items()}
    if isinstance(value, list): return [_render(v, item) for v in value]
    return value

class BatchMapNode(NodeBase):
    @classmethod
    def spec(cls):
        return NodeSpec(type_id='user/jsm_batch_map', title='批次映射', category='控制', description='标准化批次并映射条目', inputs=[PortSpec('items', PortType.JSON, label='条目列表', required=True)], outputs=[PortSpec('batch', PortType.JSON, label='批次信封'), PortSpec('report', PortType.JSON, label='批次报告')], params=[PortSpec('mapping', PortType.STRING, label='映射模板', default='', widget='text'), PortSpec('concurrency', PortType.INT, label='并发数', default=2, widget='number'), PortSpec('checkpoint', PortType.BOOL, label='保存断点', default=True, widget='toggle')], version='1.0.0')
    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        raw = inputs.get('items')
        if hasattr(raw, 'id'): raw = ctx['store'].read_json(raw.id)
        batch = batchlib.normalize_batch(raw)
        mapping = params.get('mapping') or '{}'
        try: template = json.loads(mapping) if isinstance(mapping, str) else mapping
        except Exception as exc: raise ValueError('mapping 必须是 JSON 对象') from exc
        if not isinstance(template, dict): raise ValueError('mapping 必须是 JSON 对象')
        async def worker(item):
            source = item.get('input')
            mapped = _render(template, source if isinstance(source, dict) else {'value': source})
            return mapped
        cp = ''
        if params.get('checkpoint', True): cp = os.path.join(ctx.get('workdir', '.'), 'batch_checkpoints', str(ctx.get('node_id', 'batch_map')) + '.json')
        batch = await batchlib.run_items(batch, worker, concurrency=int(params.get('concurrency', 2) or 2), checkpoint_path=cp, progress=ctx.get('progress'))
        report = batchlib.summarize(batch)
        return {'batch': batch, 'report': report}
