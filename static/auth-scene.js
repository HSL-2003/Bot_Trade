(function () {
    const host = document.getElementById('masterChiefScene');
    if (!host) return;
    const loading = host.querySelector('.scene-loader');

    if (!window.THREE || !THREE.WebGLRenderer || !THREE.GLTFLoader) {
        if (loading) loading.textContent = '3D ENGINE FAILED — REFRESH PAGE';
        console.error('3D scene dependencies missing.', {
            three: !!window.THREE,
            renderer: !!(window.THREE && THREE.WebGLRenderer),
            gltfLoader: !!(window.THREE && THREE.GLTFLoader)
        });
        return;
    }

    // 1. Scene Setup
    const scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x050a14, 0.08);

    // 2. Camera Setup
    const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 100);
    camera.position.set(0, 0.1, 4.3);
    camera.lookAt(0, 0, 0);

    // 3. Renderer Setup
    let renderer;
    try {
        renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    } catch (error) {
        if (loading) loading.textContent = 'WEBGL UNAVAILABLE';
        console.error('Unable to create WebGL renderer:', error);
        return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setClearColor(0x000000, 0);
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.25;

    // 4. Cinematic Lighting Setup
    const hemiLight = new THREE.HemisphereLight(0xb0e8ff, 0x06111f, 1.8);
    scene.add(hemiLight);

    const keyLight = new THREE.DirectionalLight(0xffffff, 4.2);
    keyLight.position.set(3, 5, 4);
    scene.add(keyLight);

    const cyanRim = new THREE.DirectionalLight(0x00f0ff, 6.5);
    cyanRim.position.set(-4, 3, -3);
    scene.add(cyanRim);

    const emeraldRim = new THREE.DirectionalLight(0x00ff9d, 4.8);
    emeraldRim.position.set(4, -1, -2);
    scene.add(emeraldRim);

    const fillLight = new THREE.DirectionalLight(0x1e3a8a, 2.0);
    fillLight.position.set(0, -3, 3);
    scene.add(fillLight);

    host.appendChild(renderer.domElement);

    // 5. Unified Character Group (Holds Master Chief + Hologram Pedestal together)
    const characterGroup = new THREE.Group();
    characterGroup.rotation.y = -0.35;
    scene.add(characterGroup);

    // Holographic Pedestal Base (Positioned at y = 0 inside characterGroup)
    const pedestalGroup = new THREE.Group();
    characterGroup.add(pedestalGroup);

    // Ring 1: Outer Hologram Ring
    const ring1Geo = new THREE.RingGeometry(0.85, 0.89, 64);
    const ring1Mat = new THREE.MeshBasicMaterial({
        color: 0x38bdf8,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: 0.8
    });
    const ring1 = new THREE.Mesh(ring1Geo, ring1Mat);
    ring1.rotation.x = Math.PI / 2;
    pedestalGroup.add(ring1);

    // Ring 2: Inner Dashed Ring
    const ring2Geo = new THREE.RingGeometry(0.65, 0.68, 48);
    const ring2Mat = new THREE.MeshBasicMaterial({
        color: 0x00ff9d,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: 0.55,
        wireframe: true
    });
    const ring2 = new THREE.Mesh(ring2Geo, ring2Mat);
    ring2.rotation.x = Math.PI / 2;
    pedestalGroup.add(ring2);

    // Glowing Platform Core Disk
    const coreDiskGeo = new THREE.CircleGeometry(0.62, 32);
    const coreDiskMat = new THREE.MeshBasicMaterial({
        color: 0x0284c7,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: 0.22
    });
    const coreDisk = new THREE.Mesh(coreDiskGeo, coreDiskMat);
    coreDisk.rotation.x = Math.PI / 2;
    pedestalGroup.add(coreDisk);

    // Invisible ground collider plane — Raycaster fires downward from foot bones and intersects this mesh
    // Must be large enough to always catch both feet regardless of walking stance width
    const groundColliderGeo = new THREE.PlaneGeometry(6, 6);
    const groundColliderMat = new THREE.MeshBasicMaterial({ visible: false, side: THREE.DoubleSide });
    const groundMesh = new THREE.Mesh(groundColliderGeo, groundColliderMat);
    groundMesh.rotation.x = -Math.PI / 2;
    groundMesh.position.y = 0;
    pedestalGroup.add(groundMesh);

    // 6. Tactical Data Particle System
    const particleCount = 150;
    const particleGeo = new THREE.BufferGeometry();
    const particlePositions = new Float32Array(particleCount * 3);
    const particleVelocities = new Float32Array(particleCount);

    for (let i = 0; i < particleCount; i++) {
        particlePositions[i * 3]     = (Math.random() - 0.5) * 4.0;
        particlePositions[i * 3 + 1] = -0.85 + Math.random() * 2.8;
        particlePositions[i * 3 + 2] = (Math.random() - 0.5) * 3.5;
        particleVelocities[i] = 0.003 + Math.random() * 0.006;
    }
    particleGeo.setAttribute('position', new THREE.BufferAttribute(particlePositions, 3));

    function createParticleTexture() {
        const canvas = document.createElement('canvas');
        canvas.width = 16; canvas.height = 16;
        const ctx = canvas.getContext('2d');
        const grad = ctx.createRadialGradient(8, 8, 0, 8, 8, 8);
        grad.addColorStop(0,   'rgba(56, 189, 248, 1)');
        grad.addColorStop(0.4, 'rgba(0, 240, 255, 0.6)');
        grad.addColorStop(1,   'rgba(0, 0, 0, 0)');
        ctx.fillStyle = grad;
        ctx.beginPath(); ctx.arc(8, 8, 8, 0, Math.PI * 2); ctx.fill();
        return new THREE.CanvasTexture(canvas);
    }

    const particleMat = new THREE.PointsMaterial({
        size: 0.085, map: createParticleTexture(),
        transparent: true, opacity: 0.85,
        blending: THREE.AdditiveBlending, depthWrite: false
    });
    const particles = new THREE.Points(particleGeo, particleMat);
    scene.add(particles);

    // 7. GLTF Model Loader & Material Enhancement
    let model     = null;
    let mixer     = null;
    let activeAction = null;

    let autoOrbit = false;

    const pointer       = new THREE.Vector2();
    const targetRotation = new THREE.Vector2(-0.02, -0.35);
    const drag          = { active: false, x: 0, y: 0 };
    const loader        = new THREE.GLTFLoader();

    const isRegister = window.location.pathname === '/register';
    const masterChief = isRegister
        ? '/static/models/halo_b_model/scene.gltf'
        : '/static/models/halo_mk_v_model/scene.gltf';
    const fallback = '/static/DamagedHelmet/DamagedHelmet.gltf';

    const statusTextEl = document.querySelector('.status-text');
    if (statusTextEl) {
        statusTextEl.textContent = isRegister
            ? 'HALO INFINITE SPARTAN // GLTF 3D MODEL'
            : 'HALO SPARTAN MK V // GLTF 3D MODEL';
    }

    function prepare(gltf, source) {
        model = gltf.scene;

        // Scale model to comfortable height (1.55 world units total)
        const initialBox  = new THREE.Box3().setFromObject(model);
        const initialSize = initialBox.getSize(new THREE.Vector3());
        const fitScale    = 1.55 / Math.max(initialSize.x, initialSize.y, initialSize.z, 0.01);
        model.scale.setScalar(fitScale);

        // Align bottom of boots to sit exactly on the pedestal at y = 0
        const box    = new THREE.Box3().setFromObject(model);
        const center = box.getCenter(new THREE.Vector3());
        model.position.x = -center.x;
        model.position.z = -center.z;
        model.position.y = -box.min.y;

        model.traverse(function (child) {
            if (!child.isMesh) return;
            child.frustumCulled = false;
            if (!child.material) return;

            const materials = Array.isArray(child.material) ? child.material : [child.material];
            materials.forEach(function (mat) {
                mat.side        = THREE.DoubleSide;
                mat.transparent = false;
                mat.depthWrite  = true;

                const matName  = (mat.name  || '').toLowerCase();
                const meshName = (child.name || '').toLowerCase();

                if (matName.includes('visor') || meshName.includes('visor') || matName.includes('glass')) {
                    // Glowing Icy Cyan Visor
                    mat.color.setHex(0x38bdf8);
                    if (mat.emissive) mat.emissive.setHex(0x0284c7);
                    if (mat.roughness  !== undefined) mat.roughness  = 0.1;
                    if (mat.metalness  !== undefined) mat.metalness  = 0.95;
                } else {
                    // Weathered Arctic Bone-White Ceramic Armor
                    mat.color.setHex(0xe2e8f0);
                    if (mat.roughness  !== undefined) mat.roughness  = 0.52;
                    if (mat.metalness  !== undefined) mat.metalness  = 0.45;
                    if (mat.normalScale) mat.normalScale.set(2.2, 2.2);
                }
                mat.needsUpdate = true;
            });
        });
        characterGroup.add(model);
        

        if (gltf.animations && gltf.animations.length) {
            mixer = new THREE.AnimationMixer(model);
            activeAction = mixer.clipAction(gltf.animations[0]);
            activeAction.setLoop(THREE.LoopRepeat).play();
        }

        if (loading) loading.style.display = 'none';
        console.info('3D asset loaded:', source);
    }

    
    // Finds the ankle/ball foot bones of whichever Spartan model is loaded
    // (halo_b: foot_l_082 / ball_l_083 · halo_mk_v: foot.L_56 / toe.L_54)
    function setupFootBones(model) {
        function pickFirst(matches) {
            let found = null;
            model.traverse(function (child) {
                if (found) return;
                const n = (child.name || '').toLowerCase();
                if (matches(n)) found = child;
            });
            return found;
        }

        function sideTest(n, side) {
            if (side === 'left')  return n.includes('_l') || n.includes('l_') || n.includes('.l') || n.includes('left');
            return n.includes('_r') || n.includes('r_') || n.includes('.r') || n.includes('right');
        }

        // Only names that START with foot/toe/heel/ball are real contact bones,
        // which excludes IK controllers like "ik_foot_l" that never animate.
        ['left', 'right'].forEach(function (side) {
            const bone = pickFirst(function (n) { return sideTest(n, side) && /^foot/.test(n); })
                      || pickFirst(function (n) { return sideTest(n, side) && /^(toe|heel|ball)/.test(n); });
            const toe  = pickFirst(function (n) { return sideTest(n, side) && /^(toe|heel|ball)/.test(n); });
            footState[side].bone    = bone || null;
            footState[side].toeBone = (toe && toe !== bone) ? toe : null;
            if (bone) console.info('Footstep impact ready — ' + side + ' foot bone:', bone.name);
        });
    }

    function handleFootstep() {
        if (!activeAction || !mixer) return;
        const clip     = activeAction.getClip();
        const duration = clip ? clip.duration : 0;
        if (!(duration > 0)) return;

        // Planted-phase windows measured from the animation data itself:
        // halo_b "Walk"   → right foot plants at 0.10, left at 0.35        (register)
        // halo_mk_v "MK VAction" → left foot steps at 0.34, right closes at 0.78 (login)
        const W = isRegister
            ? { l0: 0.28, l1: 0.42, r0: 0.05, r1: 0.17 }
            : { l0: 0.31, l1: 0.40, r0: 0.74, r1: 0.84 };
        const normTime = (activeAction.time % duration) / duration;

        let side = '';
        if (normTime >= W.l0 && normTime <= W.l1) {
            if (lastStepPhase !== 'left') { lastStepPhase = 'left'; side = 'left'; }
        } else if (normTime >= W.r0 && normTime <= W.r1) {
            if (lastStepPhase !== 'right') { lastStepPhase = 'right'; side = 'right'; }
        } else {
            lastStepPhase = ''; // reset between steps
        }

        if (side) triggerFootstepStomp(side);
    }

    function triggerFootstepStomp(side) {
        // Place the crack exactly under the striking foot via a downward Raycaster
        let hitPoint = null;
        const foot = side === 'left' ? footState.left : footState.right;
        if (foot.bone) {
            foot.bone.getWorldPosition(_footWorldPos);
            footRaycaster.set(_footWorldPos, downDirection);
            footRaycaster.far = 6;
            const hits = footRaycaster.intersectObject(groundMesh, false);
            if (hits.length) hitPoint = hits[0].point;
        }
        // Fallback: crack at the pedestal center under the character
        if (!hitPoint && characterGroup) {
            characterGroup.getWorldPosition(_footWorldPos);
            _footWorldPos.y += 0.5;
            footRaycaster.set(_footWorldPos, downDirection);
            footRaycaster.far = 6;
            const hits = footRaycaster.intersectObject(groundMesh, false);
            if (hits.length) hitPoint = hits[0].point;
        }
        spawnGroundCrack(hitPoint, side);

        // Soft camera kick fired exactly on the foot plant, then decays in the loop
        cameraShakeIntensity = 0.07;
        if (ring1Mat) ring1Mat.color.setHex(0xff7700);
        if (ring2Mat) ring2Mat.color.setHex(0xff3300);
    }

    function ensureCrackAssets() {
        if (crackTexture) return;
        crackTexture  = createCrackTexture();
        crackGeometry = new THREE.PlaneGeometry(1, 1);
    }

    function spawnGroundCrack(hitPoint, side) {
        ensureCrackAssets();
        const targetSize = side === 'left' ? 1.2 : 1.0;

        const material = new THREE.MeshBasicMaterial({
            map: crackTexture,
            transparent: true,
            opacity: 0.95,
            depthWrite: false,
            side: THREE.DoubleSide
        });

        const crack = new THREE.Mesh(crackGeometry, material);
        crack.rotation.x = -Math.PI / 2;
        crack.rotation.z = Math.random() * Math.PI * 2;
        crack.position.set(
            hitPoint ? hitPoint.x : 0,
            hitPoint ? hitPoint.y + 0.004 : -0.78,
            hitPoint ? hitPoint.z : 0
        );
        crack.scale.set(0.12, 0.12, 1);
        crack.userData = { life: 0, maxLife: 5.0, targetSize: targetSize, currentSize: 0.12 };
        scene.add(crack);
        activeCracks.push(crack);

        // Remove the oldest crack if we exceed the cap
        if (activeCracks.length > MAX_CRACKS) {
            const oldest = activeCracks.shift();
            scene.remove(oldest);
            oldest.material.dispose();
        }
    }

    function createCrackTexture() {
        const c = document.createElement('canvas');
        c.width = 256; c.height = 256;
        const ctx = c.getContext('2d');
        ctx.clearRect(0, 0, 256, 256);

        const cx = 128, cy = 128;

        // Dark jagged fracture veins radiating from the impact point, with a hot
        // molten-orange edge so the crack looks freshly smashed open
        ctx.shadowColor = 'rgba(255, 120, 20, 0.95)';
        ctx.shadowBlur  = 12;
        ctx.strokeStyle = 'rgba(10, 8, 8, 0.92)';
        ctx.lineWidth   = 5;
        ctx.lineCap     = 'round';
        ctx.lineJoin    = 'round';

        for (let i = 0; i < 11; i++) {
            const baseAngle = (i / 11) * Math.PI * 2 + Math.random() * 0.45;
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            let x = cx, y = cy;
            const segments = 3 + (i % 3);
            for (let s = 0; s < segments; s++) {
                const a = baseAngle + (Math.random() - 0.5) * 0.75;
                x += Math.cos(a) * (16 + Math.random() * 14);
                y += Math.sin(a) * (16 + Math.random() * 14);
                ctx.lineTo(x, y);
            }
            ctx.stroke();
        }

        // Impact crater core
        const crater = ctx.createRadialGradient(cx, cy, 2, cx, cy, 26);
        crater.addColorStop(0,    'rgba(5, 5, 6, 0.98)');
        crater.addColorStop(0.55, 'rgba(30, 24, 20, 0.9)');
        crater.addColorStop(0.85, 'rgba(255, 120, 20, 0.55)');
        crater.addColorStop(1,    'rgba(255, 90, 10, 0)');
        ctx.fillStyle = crater;
        ctx.beginPath(); ctx.arc(cx, cy, 26, 0, Math.PI * 2); ctx.fill();

        // Molten rim glow
        ctx.strokeStyle = 'rgba(255, 140, 40, 0.6)';
        ctx.lineWidth   = 2;
        ctx.beginPath(); ctx.arc(cx, cy, 29, 0, Math.PI * 2); ctx.stroke();

        const tex = new THREE.CanvasTexture(c);
        tex.anisotropy = 4;
        return tex;
    }

    function updateCracks(dt) {
        const POP_TIME = 0.22; // crack "bursts" open in the first frames
        for (let i = activeCracks.length - 1; i >= 0; i--) {
            const crack = activeCracks[i];
            const u = crack.userData;
            u.life += dt;

            if (u.life < POP_TIME) {
                const p = u.life / POP_TIME;
                const s = THREE.MathUtils.lerp(u.currentSize, u.targetSize, p);
                crack.scale.set(s, s, 1);
            } else {
                crack.scale.set(u.targetSize, u.targetSize, 1);
                const fade = 1 - ((u.life - POP_TIME) / (u.maxLife - POP_TIME));
                crack.material.opacity = 0.95 * Math.max(0, fade);
            }

            if (u.life >= u.maxLife) {
                scene.remove(crack);
                crack.material.dispose();
                activeCracks.splice(i, 1);
            }
        }
    }

    loader.load(masterChief, function (gltf) {
        prepare(gltf, 'Halo Master Chief GLTF');
    }, undefined, function () {
        console.warn('Master Chief GLTF failed, loading DamagedHelmet fallback.');
        loader.load(fallback, function (gltf) {
            prepare(gltf, 'DamagedHelmet fallback');
        }, undefined, function (error) {
            console.error('Unable to load 3D fallback:', error);
            if (loading) loading.textContent = '3D ASSET FAILED';
        });
    });

    // 8. Interaction Handlers & Raycasting Mesh Selection (separate raycaster for UI)
    const uiRaycaster  = new THREE.Raycaster();
    let clickTracker = { x: 0, y: 0, time: 0 };

    host.addEventListener('pointermove', function (event) {
        const rect = host.getBoundingClientRect();
        pointer.x =  ((event.clientX - rect.left) / rect.width)  * 2 - 1;
        pointer.y = -(((event.clientY - rect.top)  / rect.height) * 2 - 1);

        // Hover Raycast: Show pointer cursor ONLY when hovering directly over Master Chief's 3D mesh
        if (!drag.active && model) {
            uiRaycaster.setFromCamera(pointer, camera);
            const intersects = uiRaycaster.intersectObject(model, true);
            host.style.cursor = (intersects && intersects.length > 0) ? 'pointer' : 'grab';
        }

        if (drag.active) {
            targetRotation.y += (event.clientX - drag.x) * 0.012;
            targetRotation.x += (event.clientY - drag.y) * 0.008;
            targetRotation.x = Math.max(-0.4, Math.min(0.4, targetRotation.x));
            drag.x = event.clientX;
            drag.y = event.clientY;
        } else if (!autoOrbit) {
            targetRotation.y = -0.35 + pointer.x * 0.45;
            targetRotation.x = pointer.y * 0.15;
        }
    }, { passive: true });

    host.addEventListener('pointerdown', function (event) {
        drag.active = true;
        drag.x = event.clientX;
        drag.y = event.clientY;
        clickTracker.x    = event.clientX;
        clickTracker.y    = event.clientY;
        clickTracker.time = Date.now();
        host.setPointerCapture(event.pointerId);
    });

    host.addEventListener('pointerup', function (event) {
        drag.active = false;
        try { host.releasePointerCapture(event.pointerId); } catch (e) { }

        const dist     = Math.hypot(event.clientX - clickTracker.x, event.clientY - clickTracker.y);
        const duration = Date.now() - clickTracker.time;

        // Precision Click: Navigate to home ONLY if the user clicked directly on Master Chief's 3D mesh
        if (dist < 8 && duration < 350 && model) {
            const rect       = host.getBoundingClientRect();
            const clickMouse = new THREE.Vector2(
                 ((event.clientX - rect.left) / rect.width)  * 2 - 1,
                -(((event.clientY - rect.top)  / rect.height) * 2 - 1)
            );
            uiRaycaster.setFromCamera(clickMouse, camera);
            const intersects = uiRaycaster.intersectObject(model, true);
            if (intersects && intersects.length > 0) {
                console.info('Direct 3D Master Chief Mesh Clicked — Navigating to Landing Page...');
                window.location.href = '/';
            }
        }
    });

    // Global HUD controls expose
    window.authSceneControls = {
        resetView:    function () { targetRotation.set(-0.02, -0.35); autoOrbit = false; },
        toggleOrbit:  function () { autoOrbit = !autoOrbit; return autoOrbit; }
    };

    function resize() {
        const rect = host.getBoundingClientRect();
        camera.aspect = Math.max(rect.width, 1) / Math.max(rect.height, 1);
        camera.updateProjectionMatrix();
        renderer.setSize(Math.max(rect.width, 1), Math.max(rect.height, 1), false);
    }
    window.addEventListener('resize', resize);
    resize();

    // =====================================================================
    // 9. ANIMATION LOOP
    // =====================================================================
    const clock = new THREE.Clock();

    function animate() {
        requestAnimationFrame(animate);
        const dt = Math.min(clock.getDelta(), 0.1);

        if (mixer) mixer.update(dt);

        

        // ── Restore Pedestal Ring Colors ──────────────────────────────────
        if (ring1Mat && ring1Mat.color.getHex() !== 0x38bdf8)
            ring1Mat.color.lerp(new THREE.Color(0x38bdf8), 0.08);
        if (ring2Mat && ring2Mat.color.getHex() !== 0x00ff9d)
            ring2Mat.color.lerp(new THREE.Color(0x00ff9d), 0.08);

        // ── Rotate Holographic Pedestal ───────────────────────────────────
        ring1.rotation.z += 0.006;
        ring2.rotation.z -= 0.009;

        // ── Floating Data Particles ───────────────────────────────────────
        const pos = particleGeo.attributes.position.array;
        for (let i = 0; i < particleCount; i++) {
            pos[i*3+1] += particleVelocities[i];
            if (pos[i*3+1] > 1.9) {
                pos[i*3+1] = -0.85;
                pos[i*3]   = (Math.random() - 0.5) * 4.0;
            }
        }
        particleGeo.attributes.position.needsUpdate = true;

        

        // ── Character Group Smooth Sway & Position ────────────────────────
        if (characterGroup) {
            if (autoOrbit && !drag.active) targetRotation.y += dt * 0.5;

            const isDesktop = window.innerWidth > 960;
            const targetX   = isDesktop ? 0.42 : 0;
            const targetY   = isDesktop ? -0.82 : -0.78;

            characterGroup.position.x += (targetX - characterGroup.position.x) * 0.08;
            characterGroup.position.y += (targetY + Math.sin(clock.getElapsedTime() * 1.5) * 0.01 - characterGroup.position.y) * 0.08;
            characterGroup.position.z  = 0;

            characterGroup.rotation.y += (targetRotation.y - characterGroup.rotation.y) * 0.08;
            characterGroup.rotation.x += (targetRotation.x - characterGroup.rotation.x) * 0.08;
        }

        renderer.render(scene, camera);
    }
    animate();
}());