from __future__ import annotations
import os,re,shutil,subprocess
from typing import Any,Dict
from app.assets import Asset
from app.nodespec import NodeBase,NodeSpec,PortSpec,PortType

def ff():
 from app.plugin import deps
 r=deps.resolve_binary('ffmpeg',env=os.environ)
 return r['path'] if r.get('ready') else 'ffmpeg'
def info(p,e):
 s=(subprocess.run([e,'-hide_banner','-i',p],capture_output=True,text=True).stderr or ''); d=re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)',s); dur=(int(d.group(1))*3600+int(d.group(2))*60+float(d.group(3))) if d else 1.; b=re.search(r'bitrate:\s*(\d+(?:\.\d+)?)\s*kb/s',s); br=float(b.group(1))*1000 if b else 0; line=next((x for x in s.splitlines() if 'Video:' in x),''); w=re.search(r'(\d{2,5})x(\d{2,5})',line); return {'duration':dur,'bitrate':br,'width':int(w.group(1)) if w else 0,'height':int(w.group(2)) if w else 0}
class VideoCompressNode(NodeBase):
 @classmethod
 def spec(cls): return NodeSpec(type_id='user/jsm_video_compress',title='视频压缩',category='剪辑',description='硬件失败自动软编码回退',inputs=[PortSpec('video',PortType.VIDEO,label='源视频')],outputs=[PortSpec('compressed',PortType.VIDEO,label='压缩视频'),PortSpec('stats',PortType.JSON,label='压缩统计')],params=[PortSpec('encoder',PortType.STRING,default='auto',widget='select',options=['auto','h264','h265','nvenc_h264','nvenc_h265']),PortSpec('crf',PortType.INT,default=23,widget='number'),PortSpec('preset',PortType.STRING,default='medium',widget='select',options=['ultrafast','fast','medium','slow','veryslow']),PortSpec('max_width',PortType.INT,default=0,widget='number'),PortSpec('fps',PortType.INT,default=0,widget='number'),PortSpec('audio_bitrate',PortType.STRING,default='192k'),PortSpec('accel',PortType.STRING,default='auto',widget='select',options=['auto','none']),PortSpec('output_name',PortType.STRING,default='compressed')],version='1.0.0')
 async def run(self,ctx,inputs,params)->Dict[str,Any]:
  v=inputs.get('video');
  if not v or not v.path or not os.path.exists(v.path): raise RuntimeError('需要视频输入')
  e=ff(); inf=info(v.path,e); enc=str(params.get('encoder','auto')); crf=int(params.get('crf',23) or 23); preset=str(params.get('preset','medium')); warnings=[]
  if enc=='auto': enc='nvenc_h265' if str(params.get('accel','auto'))!='none' else 'h265'
  hw=enc.startswith('nvenc'); codec='hevc_nvenc' if enc in ('h265','nvenc_h265') and hw else 'h264_nvenc' if hw else 'libx265' if enc=='h265' else 'libx264'; eff=crf+5 if codec in ('hevc_nvenc','libx265') else crf
  out=os.path.join(ctx['workdir'],str(params.get('output_name','compressed') or 'compressed')+'.mp4'); cmd=[e,'-y','-fflags','+genpts+igndts']+(['-hwaccel','cuda'] if hw else [])+['-i',v.path,'-c:v',codec]
  cmd += (['-rc','vbr','-cq',str(eff),'-preset','p4'] if hw else ['-crf',str(eff),'-preset',preset]);
  if inf['bitrate']: cmd += ['-maxrate',str(int(inf['bitrate']*.7)),'-bufsize',str(int(inf['bitrate']*1.4))]
  if int(params.get('max_width',0) or 0): cmd += ['-vf',f"scale={int(params['max_width'])}:-2"]
  if int(params.get('fps',0) or 0): cmd += ['-r',str(int(params['fps']))]
  cmd += ['-c:a','aac','-b:a',str(params.get('audio_bitrate','192k')),'-map_metadata','-1',out]
  r=await ctx['run_tracked'](cmd,timeout=900,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  if r.returncode!=0 and hw:
   warnings.append('硬件编码失败，已自动回退软件编码'); codec='libx265' if enc in ('h265','nvenc_h265') else 'libx264'; eff=crf+5 if codec=='libx265' else crf; cmd=[e,'-y','-fflags','+genpts+igndts','-i',v.path,'-c:v',codec,'-crf',str(eff),'-preset',preset,'-c:a','aac','-b:a',str(params.get('audio_bitrate','192k')),'-map_metadata','-1',out]; r=await ctx['run_tracked'](cmd,timeout=900,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  if r.returncode!=0: raise RuntimeError('视频压缩失败: '+str(r.stderr or '')[-1000:])
  sz=os.path.getsize(out); orig=os.path.getsize(v.path); a=ctx['store'].save_asset(Asset(id='',kind='VIDEO',path=out,meta={'codec':codec})); st={'original_size':orig,'compressed_size':sz,'compression_ratio':round(sz/orig,4) if orig else 0,'saved_bytes':orig-sz,'encoder_used':codec,'crf_used':eff,'maxrate':int(inf['bitrate']*.7) if inf['bitrate'] else None,'warnings':warnings}
  return {'compressed':a,'stats':st}
