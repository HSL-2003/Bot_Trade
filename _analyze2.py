import json, struct, math, os

def load(p):
    g = json.load(open(p, encoding='utf-8'))
    b = {}
    for idx, x in enumerate(g.get('buffers', [])):
        b[idx] = open(os.path.join(os.path.dirname(p), x['uri']), 'rb').read()
    return g, b

def read_acc(g, buf, acc):
    view = g['bufferViews'][acc['bufferView']]
    data = buf[view['buffer']]
    sz = (view.get('byteOffset', 0) or 0) + (acc.get('byteOffset', 0) or 0)
    fmt = {5120:'b',5121:'B',5122:'h',5123:'H',5125:'I',5126:'f'}[acc['componentType']]
    fs = struct.calcsize(fmt)
    nc = {'SCALAR':1,'VEC2':2,'VEC3':3,'VEC4':4,'MAT4':16}[acc['type']]
    stride = view.get('byteStride', fs*nc) or (fs*nc)
    out=[]
    for i in range(acc['count']):
        row=[]
        for j in range(nc):
            (v,)=struct.unpack_from(fmt, data, sz+i*stride+j*fs)
            row.append(v)
        out.append(row)
    return out

def qmat(q):
    w,x,y,z=q
    return [[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w),0],
            [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w),0],
            [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y),0],[0,0,0,1]]

def mmul(A,B): return [[sum(A[i][k]*B[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
def trans(x,y,z): return [[1,0,0,x],[0,1,0,y],[0,0,1,z],[0,0,0,1]]
def scl(x,y,z): return [[x,0,0,0],[0,y,0,0],[0,0,z,0],[0,0,0,1]]
IDENT=[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
def world_mat(g, lerp, idx, t):
    nodes = g['nodes']
    n = nodes[idx]
    if 'matrix' in n:
        m = n['matrix']
        return [[m[0],m[1],m[2],m[3]],[m[4],m[5],m[6],m[7]],[m[8],m[9],m[10],m[11]],[m[12],m[13],m[14],m[15]]]
    tv = list(n.get('translation',[0,0,0]))
    rq = list(n.get('rotation',[0,0,0,1]))
    sv = list(n.get('scale',[1,1,1]))
    for path, sink in (('translation', lambda v: tv.__setitem__(slice(None), v)),
                       ('rotation',   lambda v: rq.__setitem__(slice(None), v)),
                       ('scale',      lambda v: sv.__setitem__(slice(None), v))):
        k = (idx, path)
        if k in lerp:
            lt, lv, isrot = lerp[k]
            sink(samp(lt, lv, t, isrot))
    return mmul(trans(*tv), mmul(qmat(rq), scl(*sv)))

def main():
    for gltf_path, aname, feet in [
        ('BOt/static/models/halo_b_model/scene.gltf', 'Walk',
         [('left',  'ball_l_083'), ('right', 'ball_r_095'),
          ('left',  'foot_l_082'), ('right', 'foot_r_094')]),
        ('BOt/static/models/halo_mk_v_model/scene.gltf', 'MK VAction',
         [('left',  'toe.L_54'),   ('right', 'toe.R_59'),
          ('left',  'foot.L_56'),  ('right', 'foot.R_61')]),
    ]:
        g, buf = load(gltf_path)
        nodes = g['nodes']
        parent = {}
        for i, n in enumerate(nodes):
            for c in (n.get('children') or []):
                parent[c] = i
        anim = next(a for a in g['animations'] if a.get('name') == aname)
        lerp = {}
        for ch in anim['channels']:
            tgt = ch['target']
            sam = anim['samplers'][ch['sampler']]
            tin = g['accessors'][sam['input']]
            tout = g['accessors'][sam['output']]
            ts = [r[0] for r in read_acc(g, buf, tin)]
            vals = read_acc(g, buf, tout)
            lerp[(tgt['node'], tgt['path'])] = (ts, vals, tout['type'] == 'VEC4')
        t0 = g['accessors'][anim['samplers'][anim['channels'][0]['sampler']]['input']]
        times = [r[0] for r in read_acc(g, buf, t0)]
        dur = max(times)
        nb = {}
        for i, n in enumerate(nodes):
            nb.setdefault(n.get('name',''), i)

        print('\n===== ' + gltf_path + ' | dur %.3f s =====' % dur)
        for side, bname in feet:
            idx = nb[bname]
            ys = []
            ts = []
            t = 0.0
            while t <= dur:
                ch = []
                i = idx
                while i in parent:
                    ch.append(i)
                    i = parent[i]
                M = IDENT
                for k in reversed(ch):
                    M = mmul(M, world_mat(g, lerp, k, t))
                ys.append(M[1][3])
                ts.append(t)
                t += 0.02
            mn = min(ys)
            plants = []
            for i in range(1, len(ys) - 1):
                if ys[i] < ys[i - 1] and ys[i] <= ys[i + 1]:
                    plants.append((ts[i], (ys[i] - mn)))
            plants.sort(key=lambda x: x[1])
            p = (plants[0][0] / dur)
            print('  %-5s %-14s minY=%.3f  deepest plant phase=%.3f  (t=%.3fs)' % (side, bname, mn, p, plants[0][0]))

def lerp(v0,v1,f,isrot):
    if isrot:
        d=sum(a*b for a,b in zip(v0,v1))
        if d<0: v1=[-x for x in v1]; d=-d
        v=[a*(1-f)+b*f for a,b in zip(v0,v1)]
        n=math.sqrt(sum(x*x for x in v))
        return [x/n for x in v]
    return [a*(1-f)+b*f for a,b in zip(v0,v1)]

def samp(ts,vals,t,isrot):
    if t<=ts[0]: return vals[0]
    if t>=ts[-1]: return vals[-1]
    i=0
    while ts[i+1]<t: i+=1
    f=(t-ts[i])/(ts[i+1]-ts[i])
    return lerp(vals[i],vals[i+1],f,isrot)

main()
