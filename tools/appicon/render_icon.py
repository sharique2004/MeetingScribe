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
# squircle superellipse mask, 880 content centered
half=440.0; n=5.0; yy,xx=np.mgrid[0:S,0:S].astype(float)
r=(np.abs((xx-511.5)/half)**n+np.abs((yy-511.5)/half)**n)**(1.0/n)
mask=np.clip((1.0-r)*half*0.5,0,1)  # soft AA edge
# aqua gradient (P3 values used as sRGB approx)
top=np.array([27,192,152.]); bot=np.array([35,255,203.])
t=np.clip((yy-(511.5-half))/(2*half),0,1)[:,:,None]
grad=top*(1-t)+bot*t
canvas[:,:,:3]=grad; canvas[:,:,3]=mask*255
# glyph: crop opaque bbox, scale to ~62% width *1.1, center + small nudge
g=decode_png("glyph.png"); a=g[:,:,3]; ys,xs=np.where(a>10)
y0,y1,x0,x1=ys.min(),ys.max(),xs.min(),xs.max(); crop=g[y0:y1+1,x0:x1+1]
gh,gw=crop.shape[:2]
target_w=int(2*half*0.62*1.1); target_h=int(target_w*gh/gw)
rg=bilinear(crop,target_w,target_h)
cx=int(511.5 - 13.6*(S/1024.)); cy=int(511.5 - 13.2*(S/1024.))  # translation (y up)
ox=cx-target_w//2; oy=cy-target_h//2
ga=(rg[:,:,3]/255.0)[:,:,None]
region=canvas[oy:oy+target_h, ox:ox+target_w, :3]
canvas[oy:oy+target_h, ox:ox+target_w, :3]=rg[:,:,:3]*ga+region*(1-ga)
# keep canvas alpha = squircle (glyph sits inside)
write_png("master_1024.png", np.clip(canvas,0,255))
print("wrote master_1024.png", canvas.shape, "glyph bbox", (y0,y1,x0,x1), "target", (target_w,target_h))
