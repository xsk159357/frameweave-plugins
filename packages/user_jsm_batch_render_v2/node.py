from __future__ import annotations
import json, os, re
from typing import Any, Dict, List
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType
from app.assets import Asset
from app import batchlib

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")

def _render_template(text, vars_map):
    def _sub(m):
        key = m.group(1)
        return str(vars_map.get(key, m.group(0)))
    return _TEMPLATE_RE.sub(_sub, text)

def _read_json(store, asset):
    if asset is None: return None
    try: return store.read_json(asset.id)
    except Exception: return None

class BatchRenderV2Node(NodeBase):
    @classmethod
    def spec(cls):
        return NodeSpec(type_id='user/jsm_batch_render_v2', title='批量出片 V2', category='控制', description='标准批次信封、断点续跑、逐条错误隔离的批量渲染', inputs=[PortSpec('video', PortType.VIDEO, label='源视频', required=True), PortSpec('segments', PortType.SEGMENTS, label='片段列表', required=True), PortSpec('items', PortType.JSON, label='批次条目', required=False)], outputs=[PortSpec('results', PortType.JSON, label='批次结果'), PortSpec('report', PortType.JSON, label='执行报告'), PortSpec('first_video', PortType.VIDEO, label='第一项成片', required=False)], params=[PortSpec('manual_items', PortType.STRING, label='矩阵条目', default='', widget='text'), PortSpec('variables', PortType.STRING, label='模板变量', default='', widget='text'), PortSpec('voice', PortType.STRING, label='音色', default='zh-CN-XiaoxiaoNeural', widget='text'), PortSpec('aspect', PortType.STRING, label='画幅', default='16:9', widget='select', options=['16:9', '9:16', '1:1']), PortSpec('resolution', PortType.STRING, label='分辨率', default='1080p', widget='select', options=['1080p', '720p', '4k']), PortSpec('fps', PortType.INT, label='帧率', default=30, widget='number'), PortSpec('output_prefix', PortType.STRING, label='输出前缀', default='batch_', widget='text'), PortSpec('max_concurrent', PortType.INT, label='最大并发', default=2, widget='number'), PortSpec('retries', PortType.INT, label='重试次数', default=1, widget='number'), PortSpec('resume', PortType.BOOL, label='断点续跑', default=True, widget='toggle')], version='1.0.0')

    async def run(self, ctx, inputs, params) -> Dict[str, Any]:
        from app.registry import get_class
        video = inputs.get('video'); segs_asset = inputs.get('segments')
        if video is None or not getattr(video, 'path', '') or not os.path.exists(video.path): raise RuntimeError('需要源视频输入')
        if segs_asset is None: raise RuntimeError('需要片段列表输入')
        store = ctx['store']; segs = _read_json(store, segs_asset) or []
        if not segs: raise RuntimeError('片段列表为空')
        global_vars = self._parse_vars(str(params.get('variables', '') or ''))
        items = self._parse_items(params, inputs, store, global_vars)
        if not items: raise RuntimeError('矩阵条目为空（填写矩阵条目或连接批次条目）')
        voice = params.get('voice', 'zh-CN-XiaoxiaoNeural')
        retries = max(0, int(params.get('retries', 1) or 0)); concurrency = int(params.get('max_concurrent', 2) or 2)
        cp = os.path.join(ctx.get('workdir', '.'), 'batch_checkpoints', str(ctx.get('node_id', 'batch_render_v2')) + '.json')
        resume = bool(params.get('resume', True))
        progress = ctx.get('progress')
        node_id = str(ctx.get('node_id', ''))

        async def worker(item):
            idx = int(item.get('index', 0)); payload = item.get('input')
            if not isinstance(payload, dict): raise RuntimeError('条目输入必须是对象')
            name = str(payload.get('name') or 'item_%d' % idx); script = str(payload.get('script') or '')
            if not script.strip(): raise RuntimeError('文案为空')
            merged = dict(global_vars); pv = payload.get('vars')
            if isinstance(pv, dict):
                for k, v in pv.items(): merged[str(k)] = str(v)
            return await self._process_item(ctx, store, video, segs, params, merged, name, script, idx, voice, node_id)

        batch = batchlib.new_batch(items, source='user/jsm_batch_render_v2', options={'voice': voice})
        batch = await batchlib.run_items(batch, worker, concurrency=concurrency, checkpoint_path=cp, progress=progress, retries=retries, resume=resume)
        report = batchlib.summarize(batch)
        first = None
        for it in batch['items']:
            if it.get('status') == 'succeeded' and isinstance(it.get('result'), dict) and it['result'].get('video_id'):
                first = store.get(it['result']['video_id']); break
        clean = []
        for it in batch['items']:
            rec = {'item_id': it.get('item_id'), 'index': it.get('index'), 'status': it.get('status'), 'name': (it.get('input') or {}).get('name'), 'error': it.get('error'), 'warnings': it.get('warnings'), 'attempt': it.get('attempt')}
            r = it.get('result')
            if isinstance(r, dict):
                rec['video_id'] = r.get('video_id'); rec['path'] = r.get('path'); rec['size'] = r.get('size'); rec['duration'] = r.get('duration')
            clean.append(rec)
        results_asset = store.save_asset(Asset(id='', kind=PortType.JSON.value, meta={'count': len(clean), 'ok': report.get('succeeded', 0)}), payload={'batch_id': batch.get('batch_id'), 'items': clean})
        return {'results': results_asset, 'report': report, 'first_video': first}

    async def _process_item(self, ctx, store, video, segs, params, vars_map, name, script, idx, voice, node_id):
        import asyncio
        from app.registry import get_class
        tts_cls = get_class('core/tts'); render_cls = get_class('core/video_render')
        script_asset = store.save_asset(Asset(id='', kind=PortType.SCRIPT.value), payload={'text': script})
        tts_out = await tts_cls().run(ctx, {'script': script_asset}, {'engine': 'edge', 'voice': voice, 'rate': '+0%', 'pitch': '+0Hz', 'output_format': 'mp3'})
        tts_asset = tts_out['audio']
        subs = [{'start': 0.1, 'end': max(len(script) * 0.3, 2.0), 'text': script[:60], 'style': {'font': 'Microsoft YaHei', 'size': 44, 'color': '#FFFFFF', 'stroke': '#000000', 'bold': 1}}]
        sub_asset = store.save_asset(Asset(id='', kind=PortType.SUBTITLE.value), payload=subs)
        out_name = _render_template(str(params.get('output_prefix', 'batch_')) + name, vars_map) + '_' + str(idx)
        out = await asyncio.to_thread(self._render_sync, render_cls, ctx, video, segs, tts_asset, sub_asset, params, out_name, node_id)
        return {'video_id': out['rendered'].id, 'path': out['rendered'].path, 'size': os.path.getsize(out['rendered'].path) if os.path.exists(out['rendered'].path) else 0, 'name': name, 'duration': None, '_video_asset': out['rendered']}

    def _render_sync(self, render_cls, ctx, video, segs, tts_asset, sub_asset, params, out_name, node_id):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(render_cls().run(ctx, {'video': video, 'segments': segs, 'audio': tts_asset, 'styled_subtitle': sub_asset}, {'aspect': params.get('aspect', '16:9'), 'resolution': params.get('resolution', '1080p'), 'fps': int(params.get('fps', 30) or 30), 'burn_subtitle': True, 'transition': 'cut', 'output_name': out_name}))
        finally:
            loop.close()

    @staticmethod
    def _parse_vars(text):
        out = {}
        for line in str(text or '').split('\n'):
            line = line.strip()
            if not line or '=' not in line: continue
            k, v = line.split('=', 1); out[k.strip()] = v.strip()
        return out

    def _parse_items(self, params, inputs, store, global_vars):
        items: List[Dict[str, Any]] = []
        manual = str(params.get('manual_items', '') or '').strip()
        if manual:
            for line in manual.split('\n'):
                line = line.strip()
                if not line: continue
                if '|' in line: name, script = line.split('|', 1)
                else: name, script = 'item_%d' % len(items), line
                items.append({'name': name.strip(), 'script': script.strip()})
        raw = inputs.get('items')
        if raw is not None:
            data = _read_json(store, raw)
            if isinstance(data, dict) and 'items' in data: data = data.get('items')
            if isinstance(data, list): items.extend(data)
        out = []
        for it in items:
            if not isinstance(it, dict): it = {'value': it}
            merged = dict(global_vars); iv = it.get('vars')
            if isinstance(iv, dict):
                for k, v in iv.items(): merged[str(k)] = str(v)
            rec = dict(it); rec['name'] = _render_template(str(it.get('name', 'item_%d' % len(out))), merged); rec['script'] = _render_template(str(it.get('script', '')), merged); out.append(rec)
        return out
