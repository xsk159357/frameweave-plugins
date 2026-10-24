from __future__ import annotations
import os, re, shutil, subprocess
from typing import Any, Dict
from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

def _ff():
 from app.plugin import deps
 r=deps.resolve_binary('ffmpeg',env=os.environ)
 return r['path'] if r.get('ready') else 'ffmpeg'
def _probe(p,e):
 r=subprocess.run([e,'-hide_banner','-i',p],capture_output=True,text=True); s=r.stderr or ''; m=re.search(r'(\d{2,5})x(\d{2,5})',next((x for x in s.splitlines() if 'Video:' in x),'')); return (int(m.group(1)),int(m.group(2))) if m else (640,360)
class PipComposeNode(NodeBase):
 @classmethod
 def spec(cls):
  return NodeSpec(type_id='user/jsm_pip_compose',title='口播画中画合成',category='剪辑',description='画中画叠加',inputs=[PortSpec('video',PortType.VIDEO,label='主视频'),PortSpec('pip_video',PortType.VIDEO,label='画中画视频',required=False),PortSpec('pip_image',PortType.IMAGE,label='画中画图片',required=False)],outputs=[PortSpec('video_out',PortType.VIDEO,label='合成视频'),PortSpec('report',PortType.JSON,label='处理报告')],params=[PortSpec('position',PortType.STRING,default='bottom-right',widget='select',options=['top-left','top-right','bottom-left','bottom-right','center']),PortSpec('width_ratio',PortType.FLOAT,default=.35,widget='number'),PortSpec('margin',PortType.INT,default=20,widget='number'),PortSpec('border_width',PortType.INT,default=4,widget='number'),PortSpec('border_color',PortType.STRING,default='white'),PortSpec('pip_volume',PortType.FLOAT,default=0.0,widget='number'),PortSpec('output_name',PortType.STRING,default='pip_compose')],version='1.0.0')
 async def run(self,ctx,inputs,params)->Dict[str,Any]:
  main=inputs.get('video'); pv=inputs.get('pip_video'); pi=inputs.get('pip_image')
  if not main or not main.path or not os.path.exists(main.path): raise RuntimeError('需要主视频输入')
  if bool(pv) == bool(pi): raise RuntimeError('画中画视频和画中画图片必须且只能连接一个')
  e=_ff(); w,h=_probe(main.path,e); ratio=max(.05,min(.9,float(params.get('width_ratio',.35) or .35))); pw=max(2,int(w*ratio)); margin=max(0,int(params.get('margin',20) or 20)); bw=max(0,int(params.get('border_width',4) or 4)); pos=str(params.get('position','bottom-right')); color=str(params.get('border_color','white'))
  fc=f'[1:v]scale={pw}:-2:force_original_aspect_ratio=decrease,pad=iw+{2*bw}:ih+{2*bw}:x={bw}:y={bw}:color={color}[pip];[0:v][pip]overlay='
  xy={'top-left':(margin,margin),'top-right':(f'W-w-{margin}',margin),'bottom-left':(margin,f'H-h-{margin}'),'bottom-right':(f'W-w-{margin}',f'H-h-{margin}'),'center':('W-w','H-h')}.get(pos,(f'W-w-{margin}',f'H-h-{margin}'))
  fc+=f'x={xy[0]}:y={xy[1]}[vout]'; out=os.path.join(ctx['workdir'],str(params.get('output_name','pip_compose') or 'pip_compose')+'.mp4'); cmd=[e,'-y','-i',main.path]
  if pv: cmd+=['-i',pv.path]
  else: cmd+=['-loop','1','-i',pi.path]
  cmd += ['-filter_complex',fc,'-map','[vout]','-map','0:a?','-c:v','libx264','-crf','18','-pix_fmt','yuv420p','-c:a','aac','-shortest',out]
  r=await ctx['run_tracked'](cmd,timeout=900,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  if r.returncode!=0: raise RuntimeError('画中画失败: '+str(r.stderr or '')[-1000:])
  a=ctx['store'].save_asset(Asset(id='',kind='VIDEO',path=out,meta={'position':pos,'width':w,'height':h,'pip_width':pw}))
  return {'video_out':a,'report':{'position':pos,'width':w,'height':h,'pip_width':pw,'border_width':bw,'audio':'main'}}
