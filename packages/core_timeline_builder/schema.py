"""Canonical split_cam schema (S3-T11/A7)."""
REQUIRED_FIELDS=("segment_index","speed","src_start","src_end","OST","video")
def normalize_segment(row, index=0):
    r=dict(row or {}); r["segment_index"]=int(r.get("segment_index",r.get("index",index))); r["speed"]=float(r.get("speed",1.0) or 1.0); r["src_start"]=float(r.get("src_start",r.get("start",0.0)) or 0.0); r["src_end"]=float(r.get("src_end",r.get("end",r["src_start"]))) ; r["OST"]=int(bool(r.get("OST",r.get("ost",r.get("is_ost",False))))); r["video"]=str(r.get("video",r.get("video_path","")) or ""); return r
def validate_split_cam(rows):
    if not isinstance(rows,list): raise ValueError("split_cam must be a list")
    out=[normalize_segment(x,i) for i,x in enumerate(rows)]
    for r in out:
        missing=[k for k in REQUIRED_FIELDS if k not in r]
        if missing: raise ValueError("missing split_cam fields: "+','.join(missing))
        if r["src_end"] < r["src_start"]: raise ValueError("src_end < src_start")
    return out
