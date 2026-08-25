import * as THREE from 'three'
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js'
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js'
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js'
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js'
import { GammaCorrectionShader } from 'three/addons/shaders/GammaCorrectionShader.js'
import { CopyShader } from 'three/addons/shaders/CopyShader.js'

const Lerp = (a, b, t) => a + (b - a) * t
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v))
function hexToVec3 (hex) {
  const n = parseInt(hex.slice(1), 16)
  return new THREE.Vector3(((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255)
}

// ── Shared 3D simplex noise (injected into the tunnel vertex shader) ──
const SNOISE = `
vec4 permute(vec4 x){return mod(((x*34.0)+1.0)*x, 289.0);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159 - 0.85373472095314 * r;}
float snoise(vec3 v){
  const vec2 C = vec2(1.0/6.0, 1.0/3.0); const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);
  vec3 i = floor(v + dot(v, C.yyy)); vec3 x0 = v - i + dot(i, C.xxx);
  vec3 g = step(x0.yzx, x0.xyz); vec3 l = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy); vec3 i2 = max(g.xyz, l.zxy);
  vec3 x1 = x0 - i1 + 1.0 * C.xxx; vec3 x2 = x0 - i2 + 2.0 * C.xxx; vec3 x3 = x0 - 1.0 + 3.0 * C.xxx;
  i = mod(i, 289.0);
  vec4 p = permute(permute(permute(i.z + vec4(0.0, i1.z, i2.z, 1.0)) + i.y + vec4(0.0, i1.y, i2.y, 1.0)) + i.x + vec4(0.0, i1.x, i2.x, 1.0));
  float n_ = 1.0/7.0; vec3 ns = n_ * D.wyz - D.xzx;
  vec4 j = p - 49.0 * floor(p * ns.z *ns.z);
  vec4 x_ = floor(j * ns.z); vec4 y_ = floor(j - 7.0 * x_);
  vec4 x = x_ *ns.x + ns.yyyy; vec4 y = y_ *ns.x + ns.yyyy; vec4 h = 1.0 - abs(x) - abs(y);
  vec4 b0 = vec4(x.xy, y.xy); vec4 b1 = vec4(x.zw, y.zw);
  vec4 s0 = floor(b0)*2.0 + 1.0; vec4 s1 = floor(b1)*2.0 + 1.0; vec4 sh = -step(h, vec4(0.0));
  vec4 a0 = b0.xzyw + s0.xzyw*sh.xxyy; vec4 a1 = b1.xzyw + s1.xzyw*sh.zzww;
  vec3 p0 = vec3(a0.xy,h.x); vec3 p1 = vec3(a0.zw,h.y); vec3 p2 = vec3(a1.xy,h.z); vec3 p3 = vec3(a1.zw,h.w);
  vec4 norm = taylorInvSqrt(vec4(dot(p0,p0), dot(p1,p1), dot(p2, p2), dot(p3,p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
  vec4 m = max(0.5 - vec4(dot(x0,x0), dot(x1,x1), dot(x2,x2), dot(x3,x3)), 0.0); m = m * m;
  return 42.0 * dot(m*m, vec4(dot(p0,x0), dot(p1,x1), dot(p2,x2), dot(p3,x3)));
}
`
// ── Tunnel points shaders ─────────────────────────────────────────
const TUNNEL_VERTEX = `
uniform float uTime; uniform float uSize; uniform float uSwirl; uniform float uScale;
uniform vec3 uColLow; uniform vec3 uColHigh;
uniform vec3 uCursor; uniform float uRepelRadius; uniform float uRepelStrength; uniform float uActivity;
varying float vFade; varying vec3 vColor;
${SNOISE}
void main() {
  vec3 wp = vec3(position.x * 7.0, 0.0, position.z * 25.0);
  wp.x += position.y * 6.0;
  float wn = snoise(vec3(wp.x * 0.08, wp.z * 0.08, uTime * 0.15)) * 2.0;
  wn += snoise(vec3(wp.x * 0.16, wp.z * 0.16, uTime * 0.3)) * 0.8;

  float tunnelR = 12.0;
  float currentSliceRadius = sqrt(max(0.0, 17.64 - position.z * position.z));
  float maxSliceWidth = 9.2195 * currentSliceRadius;
  float normalizedX = wp.x / (maxSliceWidth + 0.001);
  float tunnelAngle = normalizedX * 3.14159265;

  float jitterAngle = snoise(vec3(position.x * 15.0, position.y * 15.0, uTime * 0.1)) * 0.35;
  float jitterZ = snoise(vec3(position.y * 15.0, position.z * 15.0, uTime * 0.1)) * 4.0;
  float ambientSwirl = snoise(vec3(position.x * 5.0, position.y * 5.0, uTime * 0.2)) * 3.0;
  tunnelAngle += jitterAngle + ambientSwirl * uSwirl;

  float dynamicR = tunnelR - wn;
  vec3 tunnelPos = vec3(dynamicR * sin(tunnelAngle), -dynamicR * cos(tunnelAngle), wp.z + jitterZ);

  vec3 finalPos = tunnelPos * uScale;
  vec4 modelPosition = modelMatrix * vec4(finalPos, 1.0);
  vec3 toP = modelPosition.xyz - uCursor;
  float cd = length(toP);
  float fall = smoothstep(uRepelRadius, 0.0, cd);
  modelPosition.xyz += normalize(toP + vec3(0.0001)) * fall * uRepelStrength * uActivity;
  vec4 mvPosition = viewMatrix * modelPosition;

  float colMix = smoothstep(-3.0, 3.0, position.y + position.x * 0.5);
  vColor = mix(uColLow, uColHigh, clamp(colMix, 0.0, 1.0));
  vFade = 1.0;

  gl_PointSize = uSize * (10.0 / -mvPosition.z);
  gl_PointSize = max(gl_PointSize, 1.5);
  gl_Position = projectionMatrix * mvPosition;
}
`

const TUNNEL_FRAGMENT = `
uniform float uOpacity; uniform float uBrightness; uniform float uAppear;
varying float vFade; varying vec3 vColor;
void main() {
  vec2 xy = gl_PointCoord - 0.5;
  float ll = length(xy);
  if (ll > 0.5) discard;
  float a = smoothstep(0.5, 0.1, ll);
  gl_FragColor = vec4(vColor * uBrightness, vFade * a * uOpacity * uAppear);
}
`

// ── Atmosphere motes shaders ──────────────────────────────────────
const ATMO_VERTEX = `
attribute float size; attribute float seed; uniform float uTime; uniform vec2 uRes;
varying float vA;
vec3 warp(vec3 p, float t){ float c=0.9,a=1.9,b=0.02,s=0.05; p*=2.;
  p.x+=c*sin(s*t+a*p.y)+t*b; p.y+=c*cos(s*t+a*p.x); p.y+=c*sin(s*t+a*p.z)+t*b;
  p.z+=c*cos(s*t+a*p.y); p.z+=c*sin(s*t+a*p.x)+t*b; p.x+=c*cos(s*t+a*p.z);
  return cos(p+vec3(1,2,4)); }
void main(){
  vec3 v = position*4.0 + warp(position, uTime)*1.2;
  vec4 mv = modelViewMatrix * vec4(v, 1.0);
  float r = length(v); float farF = 1.0 - smoothstep(5.0, 6.5, r); float nearF = smoothstep(0.0, 0.5, -mv.z);
  vA = farF * nearF;
  gl_PointSize = size * uRes.y / 900.0 / -mv.z; gl_PointSize = max(gl_PointSize, 1.0);
  gl_Position = projectionMatrix * mv;
}
`

const ATMO_FRAGMENT = `
uniform vec3 uColor; varying float vA;
void main(){ vec2 p = gl_PointCoord - 0.5; float l = length(p); if (l > 0.5) discard;
  float tex = smoothstep(0.5, 0.0, l); gl_FragColor = vec4(uColor * tex, tex * vA * 0.6); }
`
// ── Final compositing pass (background + corner flames + bloom layering) ──
const FinalPass = {
  uniforms: {
    iTime: { value: 0 },
    tDiffuse: { value: null },
    torusTexture: { value: null },
    bloomTexture: { value: null },
    haloTexture: { value: null },
    uBg: { value: hexToVec3('#0a0524') },
    uFlameA: { value: hexToVec3('#2bf0ff') },
    uFlameB: { value: hexToVec3('#7a3cff') },
    uFlameAmt: { value: 0.2 }
  },
  vertexShader: `varying vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position, 1.0); }`,
  fragmentShader: `
uniform float iTime; uniform sampler2D tDiffuse; uniform sampler2D bloomTexture; uniform sampler2D torusTexture; uniform sampler2D haloTexture;
uniform vec3 uBg; uniform vec3 uFlameA; uniform vec3 uFlameB; uniform float uFlameAmt;
varying vec2 vUv;
vec3 warp3d(vec3 pos, float t){ float curv=.8,a=1.9,b=0.7; pos*=2.;
  pos.x+=curv*sin(t+a*pos.y)+t*b; pos.y+=curv*cos(t+a*pos.x);
  pos.y+=curv*sin(t+a*pos.z)+t*b; pos.z+=curv*cos(t+a*pos.y);
  pos.z+=curv*sin(t+a*pos.x)+t*b; pos.x+=curv*cos(t+a*pos.z);
  return 0.5+0.5*cos(pos.xyz+vec3(1,2,4)); }
void main(){
  vec2 uv = 2.*vUv - 1.;
  vec3 w = pow(warp3d(vec3(uv.x, sin(uv.y), uv.y), iTime*1.5), vec3(1.5));
  vec3 flame = 1.5*uFlameA*w.x; flame*=w.y; flame += uFlameB*w.z;
  flame *= smoothstep(0.25, 1., abs(uv.y));
  float md = smoothstep(-0.7, 1., -uv.y*uv.x); flame *= md*md;
  vec3 bg = uBg * (1.0 - 0.4 * length(uv));
  vec3 halo = texture2D(haloTexture, vUv).xyz;
  gl_FragColor = vec4(bg + flame*uFlameAmt + texture2D(bloomTexture, vUv).xyz + texture2D(torusTexture, vUv).xyz + texture2D(tDiffuse, vUv).xyz + halo, 1.);
}
`
}

// ── Shared baked-in parameters ────────────────────────────────────
const LAYERS = { NONE: 0, TORUS_SCENE: 1, BLOOM_SCENE: 2, ENTIRE_SCENE: 3 }
const PARAMS = {
  bgColor: '#0a0524',
  flameColor: '#2bf0ff', flameColor2: '#7a3cff', flameAmt: 0.2,
  atmoColor: '#8fe6ff', atmoCount: 300, atmoSize: 24, atmoSpeed: 1.0,
  colorLow: '#180a3a', colorHigh: '#2bf0ff',
  opacity: 1.44, pointSize: 5, brightness: 0.4,
  swirl: 0.39, spin: 0.065, scale: 0.17,
  scrollFly: 34, scrollSwirl: 1.5, scrollRoll: 0.05,
  steer: 0.6, parallax: 0.12, pointerRadius: 2.4, pointerStrength: 0.8
}
// ── Main scene builder ────────────────────────────────────────────
export function initTunnel (canvas, opts = {}) {
  let _t0 = 0, _appearStart = 0
  const w = opts.width  != null ? opts.width  : window.innerWidth
  const h = opts.height != null ? opts.height : window.innerHeight
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const scrollHostEl = opts.scrollHostEl || null
  let scrollTarget = 0, scrollSmooth = 0, scrollCurrent = 0
  let scrollHeight = scrollHostEl ? scrollHostEl.offsetHeight : h * 5.6

  function updateScrollTarget () {
    const rawScroll = window.scrollY || document.documentElement.scrollTop || 0
    const maxScroll = Math.max(1, scrollHeight - h)
    scrollTarget = clamp(rawScroll / maxScroll, 0, 1)
  }
  updateScrollTarget()

  // ── Renderer ─────────────────────────────────────────────────────
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true })
  renderer.setPixelRatio(dpr)
  renderer.setSize(w, h, false)
  renderer.shadowMap.enabled = true
  renderer.shadowMap.type = THREE.VSMShadowMap

  // ── Scene & Camera ───────────────────────────────────────────────
  const scene = new THREE.Scene()
  scene.background = new THREE.Color(0x000000)
  scene.fog = new THREE.Fog(0x000000, 0, 15)

  const camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 400)
  camera.position.set(0, 0, 20)
  camera.layers.set(LAYERS.TORUS_SCENE)
  camera.layers.enable(LAYERS.BLOOM_SCENE)
  camera.layers.enable(LAYERS.ENTIRE_SCENE)
  scene.add(camera)

  // ── Pointer / mouse state ────────────────────────────────────────
  const POINTER = { active: false, world: new THREE.Vector3(), activity: 0, lastMove: performance.now() }
  const mouseTarget = new THREE.Vector2(0, 0)
  const mouse = new THREE.Vector2(0, 0)

  // ── Tunnel Points ────────────────────────────────────────────────
  const tunnelGeo = new THREE.SphereGeometry(4.2, 200, 600)
  const uniforms = {
    uTime: { value: 0 }, uAppear: { value: 0 },
    uColLow: { value: hexToVec3(PARAMS.colorLow) },
    uColHigh: { value: hexToVec3(PARAMS.colorHigh) },
    uOpacity: { value: PARAMS.opacity },
    uSize: { value: PARAMS.pointSize },
    uBrightness: { value: PARAMS.brightness },
    uSwirl: { value: PARAMS.swirl },
    uScale: { value: PARAMS.scale },
    uCursor: { value: new THREE.Vector3() },
    uRepelRadius: { value: PARAMS.pointerRadius },
    uRepelStrength: { value: PARAMS.pointerStrength },
    uActivity: { value: 0 }
  }
  const tunnelMat = new THREE.ShaderMaterial({
    uniforms, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: TUNNEL_VERTEX, fragmentShader: TUNNEL_FRAGMENT
  })
  const tunnelPoints = new THREE.Points(tunnelGeo, tunnelMat)
  tunnelPoints.frustumCulled = false
  tunnelPoints.layers.set(LAYERS.ENTIRE_SCENE)

  const tunnelGroup = new THREE.Group()
  tunnelGroup.add(tunnelPoints)
  scene.add(tunnelGroup)
// ── Atmosphere Points ────────────────────────────────────────────
  const atmoN = 300
  const atmoPos = new Float32Array(atmoN * 3)
  const atmoSizes = new Float32Array(atmoN)
  const atmoSeeds = new Float32Array(atmoN)
  for (let i = 0; i < atmoN; i++) {
    atmoPos[i * 3] = 2 * Math.random() - 1
    atmoPos[i * 3 + 1] = 2 * Math.random() - 1
    atmoPos[i * 3 + 2] = 2 * Math.random() - 1
    atmoSizes[i] = 24 * (0.4 + Math.random())
    atmoSeeds[i] = Math.random()
  }
  const atmoGeo = new THREE.BufferGeometry()
  atmoGeo.setAttribute('position', new THREE.Float32BufferAttribute(atmoPos, 3))
  atmoGeo.setAttribute('size', new THREE.Float32BufferAttribute(atmoSizes, 1))
  atmoGeo.setAttribute('seed', new THREE.Float32BufferAttribute(atmoSeeds, 1))
  const atmoMat = new THREE.ShaderMaterial({
    transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, depthTest: false,
    uniforms: {
      uTime: { value: 0 }, uColor: { value: hexToVec3(PARAMS.atmoColor) },
      uRes: { value: new THREE.Vector2(w * dpr, h * dpr) }
    },
    vertexShader: ATMO_VERTEX, fragmentShader: ATMO_FRAGMENT
  })
  const atmoPoints = new THREE.Points(atmoGeo, atmoMat)
  atmoPoints.frustumCulled = false
  atmoPoints.layers.set(LAYERS.ENTIRE_SCENE)
  scene.add(atmoPoints)

  // ── Postprocessing ───────────────────────────────────────────────
  const renderScenePass = new RenderPass(scene, camera)

  const torusComposer = new EffectComposer(renderer)
  torusComposer.renderToScreen = false
  torusComposer.addPass(renderScenePass)
  torusComposer.addPass(new ShaderPass(GammaCorrectionShader))
  torusComposer.addPass(new UnrealBloomPass(new THREE.Vector2(w, h), 0.22, 0.2, 0))
  torusComposer.addPass(new ShaderPass(CopyShader))

  const bloomComposer = new EffectComposer(renderer)
  bloomComposer.renderToScreen = false
  bloomComposer.addPass(renderScenePass)
  bloomComposer.addPass(new UnrealBloomPass(new THREE.Vector2(w, h), 0.7, 0.6, 0))
  bloomComposer.addPass(new ShaderPass(GammaCorrectionShader))

  const finalPass = new ShaderPass(FinalPass)
  finalPass.uniforms.bloomTexture.value = bloomComposer.renderTarget1.texture
  finalPass.uniforms.torusTexture.value = torusComposer.renderTarget1.texture

  const finalComposer = new EffectComposer(renderer)
  finalComposer.addPass(renderScenePass)
  finalComposer.addPass(finalPass)
  let rollPhase = 0
// ── Events ───────────────────────────────────────────────────────
  window.addEventListener('scroll', updateScrollTarget, { passive: true })

  function onResize () {
    const nw = window.innerWidth, nh = window.innerHeight
    const ndpr = window.devicePixelRatio || 1
    renderer.setPixelRatio(ndpr)
    renderer.setSize(nw, nh, false)
    camera.aspect = nw / nh
    camera.updateProjectionMatrix()
    torusComposer.setPixelRatio(ndpr); torusComposer.setSize(nw, nh)
    bloomComposer.setPixelRatio(ndpr); bloomComposer.setSize(nw, nh)
    finalComposer.setPixelRatio(ndpr); finalComposer.setSize(nw, nh)
    atmoMat.uniforms.uRes.value.set(nw * ndpr, nh * ndpr)
    if (scrollHostEl) scrollHeight = scrollHostEl.offsetHeight
    updateScrollTarget()
  }
  window.addEventListener('resize', onResize)

  // ── Pointer tracking ─────────────────────────────────────────────
  const _ndc = new THREE.Vector3(), _dir = new THREE.Vector3(), _tgt = new THREE.Vector3()
  function updatePointerWorld () {
    _tgt.set(0, 0, 0)
    if (POINTER.active) {
      _ndc.set(mouse.x, mouse.y, 0.5).unproject(camera)
      _dir.copy(_ndc).sub(camera.position).normalize()
      const dn = _dir.z
      if (Math.abs(dn) > 1e-4) {
        const tt = -camera.position.z / dn
        if (tt > 0 && Number.isFinite(tt)) _tgt.copy(camera.position).addScaledVector(_dir, tt)
      }
    }
    POINTER.world.lerp(_tgt, 0.12)
    const idle = (performance.now() - POINTER.lastMove) / 1000
    POINTER.activity += (((POINTER.active && idle < 3) ? 1 : 0) - POINTER.activity) * 0.06
  }

  document.addEventListener('mousemove', function (e) {
    mouseTarget.x = (e.clientX / window.innerWidth) * 2 - 1
    mouseTarget.y = -(e.clientY / window.innerHeight) * 2 + 1
    POINTER.active = true
    POINTER.lastMove = performance.now()
  })
  document.addEventListener('mouseout', function () { POINTER.active = false })
// ── Scene update per frame ───────────────────────────────────────
  function updateScene (scroll, m) {
    const t = performance.now() / 1000
    const dt = Math.min(0.05, t - _t0)
    _t0 = t
    if (!_appearStart) { _appearStart = t; _t0 = t }

    uniforms.uTime.value = t

    camera.position.set(
      m.x * PARAMS.parallax,
      m.y * PARAMS.parallax,
      20 - scroll * PARAMS.scrollFly
    )
    camera.lookAt(
      m.x * PARAMS.steer,
      m.y * PARAMS.steer,
      camera.position.z - 12
    )

    updatePointerWorld()

    uniforms.uSwirl.value = PARAMS.swirl * (1 + scroll * PARAMS.scrollSwirl)
    rollPhase += dt * (PARAMS.spin + scroll * PARAMS.scrollRoll)
    tunnelGroup.rotation.z = rollPhase

    uniforms.uCursor.value.copy(POINTER.world)
    uniforms.uActivity.value = POINTER.activity

    const elapsed = (t - _appearStart)
    uniforms.uAppear.value = Math.max(0, Math.min(1, (elapsed - 0.2) / 1.4))

    const atmoT = t * PARAMS.atmoSpeed * 8.0
    atmoMat.uniforms.uTime.value = atmoT
    atmoPoints.position.copy(camera.position)

    finalPass.uniforms.iTime.value = t
  }

  // ── Render loop ──────────────────────────────────────────────────
  function render () {
    requestAnimationFrame(render)
    scrollSmooth = Lerp(scrollSmooth, scrollTarget, 0.10)
    scrollCurrent = Lerp(scrollCurrent, scrollSmooth, 0.06)
    mouse.x = Lerp(mouse.x, mouseTarget.x, 0.06)
    mouse.y = Lerp(mouse.y, mouseTarget.y, 0.06)

    updateScene(scrollCurrent, mouse)

    camera.layers.set(LAYERS.TORUS_SCENE); torusComposer.render()
    camera.layers.set(LAYERS.BLOOM_SCENE); bloomComposer.render()
    camera.layers.set(LAYERS.ENTIRE_SCENE); finalComposer.render()
  }

  render()

  return {
    scene, camera, renderer,
    dispose: function () {
      tunnelGeo.dispose(); tunnelMat.dispose()
      atmoGeo.dispose(); atmoMat.dispose()
      renderer.dispose()
    },
    setScrollHeight: function (h) { scrollHeight = h },
    getScroll: function () { return scrollCurrent },
    setScroll: function (s, instant = false) {
      scrollTarget = s
      if (instant) {
        scrollSmooth = s
        scrollCurrent = s
      }
    }
  }
}