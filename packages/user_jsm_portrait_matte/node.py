from __future__ import annotations
import os, re, shutil, subprocess, tempfile
from typing import Any, Dict
from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

def ffmpeg():
    from app.plugin import deps
    r=deps.resolve_binary('ffmpeg',env=os.environ)
    if r.get('ready'): return r['path']
    raise RuntimeError(deps.coded_message('binary_missing','ffmpeg',r.get('repair_hint')))

def probe(path, exe):
    p=subprocess.run([exe,'-hide_banner','-i',path],capture_output=True,text=True)
    s=p.stderr or ''
    m=re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)',s)
    d=float(m.group(1))*3600+float(m.group(2))*60+float(m.group(3)) if m else 1.0
    v=next((x for x in s.splitlines() if 'Video:' in x), '')
    wh=re.search(r'(\d{2,5})x(\d{2,5})',v); w,h=(int(wh.group(1)),int(wh.group(2))) if wh else (640,360)
    fm=re.search(r'(\d+(?:\.\d+)?)\s*fps',v); fps=float(fm.group(1)) if fm else 25.0
    return {'duration':d,'width':w,'height':h,'fps':fps,'video_stream':v}

def hexcolor(x):
    x=str(x or '#00FF00').strip().lstrip('#')
    if len(x)!=6 or not re.fullmatch(r'[0-9a-fA-F]{6}',x): raise ValueError('键色须为 #RRGGBB')
    return '0x'+x.upper(), '#'+x.upper()

class PortraitMatteNode(NodeBase):
    @classmethod
    def spec(cls):
        return NodeSpec(type_id='user/jsm_portrait_matte',title='人像抠像/绿幕合成',category='剪辑',description='人像抠像与背景替换',inputs=[PortSpec('video',PortType.VIDEO,label='源视频'),PortSpec('background',PortType.IMAGE,label='背景图',required=False)],outputs=[PortSpec('video_out',PortType.VIDEO,label='合成视频'),PortSpec('mask',PortType.IMAGE,label='人像蒙版图'),PortSpec('report',PortType.JSON,label='处理报告')],params=[PortSpec('matte_mode',PortType.STRING,default='chroma',widget='select',options=['chroma','auto','ai']),PortSpec('key_color',PortType.STRING,default='#00FF00',required=False),PortSpec('similarity',PortType.FLOAT,default=.12,widget='number',required=False),PortSpec('blend',PortType.FLOAT,default=.08,widget='number',required=False),PortSpec('despill',PortType.BOOL,default=True,widget='toggle',required=False),PortSpec('edge_feather',PortType.FLOAT,default=1.0,widget='number',required=False),PortSpec('temporal_smooth',PortType.BOOL,default=False,widget='toggle',required=False),PortSpec('keep_audio',PortType.BOOL,default=True,widget='toggle',required=False),PortSpec('mask_time',PortType.FLOAT,default=.5,widget='number',required=False),PortSpec('output_name',PortType.STRING,default='portrait_matte',required=False)],version='1.0.0')
    async def run(self,ctx,inputs,params)->Dict[str,Any]:
        src=inputs.get('video'); bg=inputs.get('background'); store=ctx['store']
        if not src or not src.path or not os.path.exists(src.path): raise RuntimeError('需要有效源视频输入')
        if str(params.get('matte_mode','chroma'))=='ai':
            try: import rembg
            except Exception as e:
                from app.plugin import deps
                raise RuntimeError(deps.coded_message(
                    'dependency_missing','rembg',
                    hint='AI 人像分割需要安装 rembg（pip install rembg）；可改用 chroma/auto 模式: '+str(e)))
            raise RuntimeError('AI 模式需专用模型管线，当前请选择 chroma 或 auto 模式')
        exe=ffmpeg(); meta=probe(src.path,exe); mode=str(params.get('matte_mode','chroma'))
        color, color_hex=hexcolor(params.get('key_color','#00FF00'))
        warnings=[]
        if mode=='auto':
            warnings.append('auto 模式使用参数键色；建议纯色背景时手动校准 key_color')
        sim=max(.01,min(.99,float(params.get('similarity',.12) or .12))); blend=max(0,min(.99,float(params.get('blend',.08) or .08)))
        feather=max(0,float(params.get('edge_feather',1.0) or 0)); key=f"chromakey=color={color}:similarity={sim:.4f}:blend={blend:.4f}"
        if params.get('despill',True): key += ',despill=green'
        fc='[0:v]'+key+'[ck];[ck]format=rgba,split=2[k1][k2];[k2]alphaextract'
        if feather: fc+=f',boxblur={min(feather,20):.2f}:1'
        fc+='[a];[k1][a]alphamerge[fg0];[fg0]split=2[fg][maskbase];[maskbase]alphaextract[maskraw];'
        if bg and bg.path and os.path.exists(bg.path):
            fc+='[1:v]scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,format=rgb24[base];[base][fg]overlay=shortest=1'% (meta['width'],meta['height'],meta['width'],meta['height'])
        else:
            fc+='color=c=black:s=%dx%d:r=%g[base];[base][fg]overlay=shortest=1'%(meta['width'],meta['height'],meta['fps'])
        if params.get('temporal_smooth',False): fc+=',tmedian=radius=1'
        fc+='[vout];[maskraw]format=gray[maskv]'
        outname=str(params.get('output_name','portrait_matte') or 'portrait_matte'); out=os.path.join(ctx['workdir'],outname+'.mp4'); maskp=os.path.join(ctx['workdir'],outname+'_mask.png')
        cmd=[exe,'-y','-i',src.path]
        if bg and bg.path and os.path.exists(bg.path): cmd+=['-loop','1','-i',bg.path]
        cmd += ['-filter_complex',fc,'-map','[vout]','-c:v','libx264','-crf','18','-pix_fmt','yuv420p']
        if params.get('keep_audio',True): cmd += ['-map','0:a?','-c:a','aac','-shortest']
        cmd += ['-map','[maskv]','-frames:v','1','-y',out,'-y',maskp]
        # ffmpeg accepts multiple outputs; split maps and codecs explicitly
        cmd=[exe,'-y','-i',src.path]+(['-loop','1','-i',bg.path] if bg and bg.path and os.path.exists(bg.path) else [])+['-filter_complex',fc,'-map','[vout]','-c:v','libx264','-crf','18','-pix_fmt','yuv420p']
        if params.get('keep_audio',True): cmd+=['-map','0:a?','-c:a','aac','-shortest']
        cmd += [out,'-map','[maskv]','-frames:v','1',maskp]
        r=await ctx['run_tracked'](cmd,timeout=900,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        if r.returncode!=0: raise RuntimeError('抠像失败: '+str(r.stderr or '')[-1200:])
        va=store.save_asset(Asset(id='',kind=PortType.VIDEO.value,path=out,meta={'mode':mode,'key_color':color_hex,'width':meta['width'],'height':meta['height']}))
        ma=store.save_asset(Asset(id='',kind=PortType.IMAGE.value,path=maskp,meta={'white_foreground':True,'key_color':color_hex}))
        report={'mode':mode,'key_color':color_hex,'similarity':sim,'blend':blend,'edge_feather':feather,'warnings':warnings,'source':meta,'background_replaced':bool(bg and bg.path)}
        return {'video_out':va,'mask':ma,'report':report}
