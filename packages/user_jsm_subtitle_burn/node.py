from __future__ import annotations
import os, shutil, subprocess
from typing import Any, Dict
from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

def _ff():
 from app.plugin import deps
 r=deps.resolve_binary('ffmpeg',env=os.environ)
 return r['path'] if r.get('ready') else 'ffmpeg'
def _ass_color(x):
 x=str(x).lstrip('#'); x=(x+'FFFFFF')[:6]; return '&H00'+x[4:6]+x[2:4]+x[:2]
def _esc(p): return p.replace('\\','/').replace(':','\\:').replace("'","\\'")
STYLES={'经典-白字黑边':('#FFFFFF','#000000',1,0),'综艺-黄字黑边':('#FFD700','#000000',1,1),'影视-红字白边':('#FF3333','#FFFFFF',1,1),'霓虹-青蓝光':('#00FFFF','#0044AA',2,1),'阅读-黑底白字':('#FFFFFF','#000000',3,1),'沉稳-黑字白边':('#000000','#FFFFFF',1,0)}
class SubtitleBurnNode(NodeBase):
 @classmethod
 def spec(cls):
  return NodeSpec(type_id='user/jsm_subtitle_burn',title='口播字幕烧录',category='剪辑',description='六套字幕样式',inputs=[PortSpec('video',PortType.VIDEO,label='源视频'),PortSpec('subtitles',PortType.SUBTITLE,label='字幕列表')],outputs=[PortSpec('video_out',PortType.VIDEO,label='字幕视频'),PortSpec('report',PortType.JSON,label='处理报告')],params=[PortSpec('style',PortType.STRING,default='经典-白字黑边',widget='select',options=list(STYLES)),PortSpec('font',PortType.STRING,default='Microsoft YaHei'),PortSpec('size',PortType.INT,default=48,widget='number'),PortSpec('position',PortType.STRING,default='bottom',widget='select',options=['bottom','top','center']),PortSpec('margin_v',PortType.INT,default=60,widget='number'),PortSpec('outline',PortType.INT,default=3,widget='number'),PortSpec('output_name',PortType.STRING,default='subtitle_burn')],version='1.0.0')
 async def run(self,ctx,inputs,params)->Dict[str,Any]:
  v=inputs.get('video'); s=inputs.get('subtitles'); store=ctx['store']
  if not v or not v.path or not os.path.exists(v.path): raise RuntimeError('需要视频输入')
  if not s: raise RuntimeError('需要字幕输入')
  rows=store.read_json(s.id) or []
  if isinstance(rows,dict): rows=rows.get('items') or rows.get('subtitles') or []
  if not isinstance(rows,list) or not rows: raise RuntimeError('字幕列表为空')
  style=str(params.get('style','经典-白字黑边')); primary,outline,border,bold=STYLES.get(style,STYLES['经典-白字黑边']); font=str(params.get('font','Microsoft YaHei')); size=max(8,int(params.get('size',48) or 48)); pos={'bottom':2,'top':8,'center':5}.get(str(params.get('position','bottom')),2); margin=max(0,int(params.get('margin_v',60) or 60)); ow=max(0,int(params.get('outline',3) or 3))
  ass=['[Script Info]','ScriptType: v4.00+','PlayResX: 1920','PlayResY: 1080','','[V4+ Styles]','Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding']
  ass.append('Style: FW,%s,%d,%s,%s,%s,%s,%d,0,0,0,100,100,0,0,%d,%d,%d,%d,40,40,%d,1'%(font,size,_ass_color(primary),_ass_color(primary),_ass_color(outline),'&HAA000000',bold,border,ow,2 if style=='霓虹-青蓝光' else 0,pos,margin))
  ass += ['','[Events]','Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
  def ts(t):
   t=max(0,float(t or 0)); return f'{int(t//3600)}:{int(t%3600//60):02d}:{t%60:05.2f}'
  for row in rows:
   if not isinstance(row,dict): continue
   st=float(row.get('start',row.get('start_sec',0)) or 0); en=float(row.get('end',row.get('end_sec',st+.1)) or st+.1); txt=str(row.get('text','')).replace('\\','\\N').replace('{','(').replace('}',')').replace('\n','\\N')
   if not txt: continue
   ass.append(f'Dialogue: 0,{ts(st)},{ts(max(en,st+.05))},FW,,0,0,0,,{txt}')
  assfile=os.path.join(ctx['workdir'],'subtitles.ass'); open(assfile,'w',encoding='utf-8').write('\n'.join(ass)); out=os.path.join(ctx['workdir'],str(params.get('output_name','subtitle_burn') or 'subtitle_burn')+'.mp4'); fc=f"ass=filename='{_esc(assfile)}'"
  cmd=[_ff(),'-y','-i',v.path,'-vf',fc,'-map','0:v:0','-map','0:a?','-c:v','libx264','-crf','18','-pix_fmt','yuv420p','-c:a','aac','-shortest',out]
  r=await ctx['run_tracked'](cmd,timeout=900,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  if r.returncode!=0: raise RuntimeError('字幕烧录失败: '+str(r.stderr or '')[-1000:])
  a=store.save_asset(Asset(id='',kind='VIDEO',path=out,meta={'style':style,'rows':len(rows)}))
  return {'video_out':a,'report':{'style':style,'rows':len(rows),'font':font,'size':size,'ass_path':assfile}}
