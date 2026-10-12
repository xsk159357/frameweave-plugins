from __future__ import annotations
import os,shutil,subprocess
from typing import Any,Dict
from app.assets import Asset
from app.nodespec import NodeBase,NodeSpec,PortSpec,PortType

def _ff():
 from app.plugin import deps
 r=deps.resolve_binary('ffmpeg',env=os.environ)
 return r['path'] if r.get('ready') else 'ffmpeg'
VC={'copy':'copy','h264':'libx264','h265':'libx265','vp9':'libvpx-vp9','av1':'libaom-av1','prores':'prores_ks'}; AC={'copy':'copy','aac':'aac','opus':'libopus','mp3':'libmp3lame','flac':'flac'}
class VideoTranscodeNode(NodeBase):
 @classmethod
 def spec(cls): return NodeSpec(type_id='user/jsm_video_transcode',title='视频转码',category='剪辑',description='容器/编码/画幅转码',inputs=[PortSpec('video',PortType.VIDEO,label='源视频')],outputs=[PortSpec('transcoded',PortType.VIDEO,label='转码视频'),PortSpec('stats',PortType.JSON,label='转码统计')],params=[PortSpec('container',PortType.STRING,default='mp4',widget='select',options=['mp4','mkv','webm','mov','avi','ts']),PortSpec('video_codec',PortType.STRING,default='h264',widget='select',options=list(VC)),PortSpec('audio_codec',PortType.STRING,default='aac',widget='select',options=list(AC)+['none']),PortSpec('crf',PortType.INT,default=23,widget='number'),PortSpec('preset',PortType.STRING,default='medium',widget='select',options=['fast','medium','slow']),PortSpec('max_width',PortType.INT,default=0,widget='number'),PortSpec('fps',PortType.INT,default=0,widget='number'),PortSpec('output_name',PortType.STRING,default='transcoded')],version='1.0.0')
 async def run(self,ctx,inputs,params)->Dict[str,Any]:
  v=inputs.get('video');
  if not v or not v.path or not os.path.exists(v.path): raise RuntimeError('需要视频输入')
  c=str(params.get('container','mp4')); vc=str(params.get('video_codec','h264')); ac=str(params.get('audio_codec','aac'))
  if c=='webm' and vc not in ('vp9','av1','copy'): raise RuntimeError('webm 容器需要 vp9/av1 编码')
  e=_ff(); out=os.path.join(ctx['workdir'],str(params.get('output_name','transcoded') or 'transcoded')+'.'+c); cmd=[e,'-y','-i',v.path,'-map','0:v:0','-c:v',VC.get(vc,vc)]
  if vc!='copy': cmd += ['-crf',str(int(params.get('crf',23) or 23)),'-preset',str(params.get('preset','medium'))]
  if int(params.get('max_width',0) or 0): cmd += ['-vf',f"scale={int(params['max_width'])}:-2"]
  if int(params.get('fps',0) or 0): cmd += ['-r',str(int(params['fps']))]
  if ac!='none': cmd += ['-map','0:a?','-c:a',AC.get(ac,ac)]
  cmd += ['-map_metadata','-1'];
  if c=='mp4': cmd += ['-movflags','+faststart']
  cmd += [out]; r=await ctx['run_tracked'](cmd,timeout=900,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  if r.returncode!=0: raise RuntimeError('视频转码失败: '+str(r.stderr or '')[-1000:])
  a=ctx['store'].save_asset(Asset(id='',kind='VIDEO',path=out,meta={'container':c,'video_codec':vc,'audio_codec':ac})); return {'transcoded':a,'stats':{'container':c,'video_codec':vc,'audio_codec':ac,'size':os.path.getsize(out)}}
