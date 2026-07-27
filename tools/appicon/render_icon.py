"""Render tools/appicon/MeetingScribe.icns from the Icon Composer source.

Renders the DARK appearance defined in MeetingScribe.icon/icon.json (charcoal
gradient + solid-aqua glyph) as a static icns. Regenerate with:
  python tools/appicon/render_icon.py   (writes master_dark_1024.png; then
  sips/iconutil produce the icns as in tools/build_mac_app.sh history)
"""
import struct, zlib, numpy as np
def decode_png(path):
    d=open(path,'rb').read(); i=8; idat=b''; W=H=ct=None
    while i<len(d):
        ln=struct.unpack('>I',d[i:i+4])[0]; tag=d[i+4:i+8]; body=d[i+8:i+8+ln]; i+=12+ln
        if tag==b'IHDR': W,H,bd,ct=struct.unpack('>IIBB',body[:10])
        elif tag==b'IDAT': idat+=body
        elif tag==b'IEND': break
    ch={0:1,2:3,3:1,4:2,6:4}[ct]; raw=zlib.decompress(idat); stride=W*ch
    out=np.zeros((H,W,ch),np.int32); prev=np.zeros(stride,np.int32); p=0
    for y in range(H):
        f=raw[p]; line=np.frombuffer(raw[p+1:p+1+stride],np.uint8).astype(np.int32); p+=1+stride
        rec=np.zeros(stride,np.int32)
        for x in range(stride):
            a=rec[x-ch] if x>=ch else 0; b=prev[x]; c=prev[x-ch] if x>=ch else 0; v=line[x]
            if f==1:v+=a
            elif f==2:v+=b
            elif f==3:v+=(a+b)//2
            elif f==4:
                pp=a+b-c;pa=abs(pp-a);pb=abs(pp-b);pc=abs(pp-c)
                v+=a if(pa<=pb and pa<=pc) else (b if pb<=pc else c)
            rec[x]=v&255
        out[y]=rec.reshape(W,ch); prev=rec
    return out.astype(np.float64)
def write_png(path, rgba):
    h,w,_=rgba.shape; rgba=rgba.astype(np.uint8)
    raw=b"".join(b"\x00"+rgba[y].tobytes() for y in range(h))
    def chunk(t,d): b=t+d; return struct.pack(">I",len(d))+b+struct.pack(">I",zlib.crc32(b))
    png=b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",w,h,8,6,0,0,0))+chunk(b"IDAT",zlib.compress(raw,9))+chunk(b"IEND",b"")
    open(path,"wb").write(png)
def bilinear(img,nw,nh):
    h,w,c=img.shape; ys=(np.arange(nh)+0.5)*h/nh-0.5; xs=(np.arange(nw)+0.5)*w/nw-0.5
    y0=np.clip(np.floor(ys),0,h-1).astype(int); x0=np.clip(np.floor(xs),0,w-1).astype(int)
    y1=np.clip(y0+1,0,h-1); x1=np.clip(x0+1,0,w-1); wy=(ys-y0)[:,None,None]; wx=(xs-x0)[None,:,None]
    Ia=img[y0][:,x0]; Ib=img[y0][:,x1]; Ic=img[y1][:,x0]; Id=img[y1][:,x1]
    return (Ia*(1-wx)*(1-wy)+Ib*wx*(1-wy)+Ic*(1-wx)*wy+Id*wx*wy)



S=1024; canvas=np.zeros((S,S,4))
half=440.0; n=5.0; yy,xx=np.mgrid[0:S,0:S].astype(float)
r=(np.abs((xx-511.5)/half)**n+np.abs((yy-511.5)/half)**n)**(1.0/n)
mask=np.clip((1.0-r)*half*0.5,0,1)
# DARK appearance per icon.json: display-p3 0.17035 gray -> extended-gray 0 (black)
top=np.array([43.,43.,43.]); bot=np.array([0.,0.,0.])
t=np.clip((yy-(511.5-half))/(2*half),0,1)[:,:,None]
canvas[:,:,:3]=top*(1-t)+bot*t; canvas[:,:,3]=mask*255
# glyph: alpha mask filled SOLID AQUA display-p3 (0.10941,0.77258,0.61076) -> ~(28,197,156)
g=decode_png("glyph.png"); a=g[:,:,3]; ys,xs=np.where(a>10)
y0,y1,x0,x1=ys.min(),ys.max(),xs.min(),xs.max(); crop=g[y0:y1+1,x0:x1+1]
gh,gw=crop.shape[:2]
tw=int(2*half*0.62*1.1); th=int(tw*gh/gw)
rg=bilinear(crop,tw,th)
aqua=np.array([28.,197.,156.])
ga=(rg[:,:,3]/255.0)[:,:,None]
cx=int(511.5-13.6); cy=int(511.5-13.2); ox=cx-tw//2; oy=cy-th//2
region=canvas[oy:oy+th, ox:ox+tw, :3]
canvas[oy:oy+th, ox:ox+tw, :3]=aqua*ga+region*(1-ga)
write_png("master_dark_1024.png", np.clip(canvas,0,255))
print("wrote master_dark_1024.png")
