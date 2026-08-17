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

    // 6. Tactical Data Particle System
    const particleCount = 150;
    const particleGeo = new THREE.BufferGeometry();
    const particlePositions = new Float32Array(particleCount * 3);
    const particleVelocities = new Float32Array(particleCount);

    for (let i = 0; i < particleCount; i++) {
        particlePositions[i * 3] = (Math.random() - 0.5) * 4.0;
        particlePositions[i * 3 + 1] = -0.85 + Math.random() * 2.8;
        particlePositions[i * 3 + 2] = (Math.random() - 0.5) * 3.5;
        particleVelocities[i] = 0.003 + Math.random() * 0.006;
    }

    particleGeo.setAttribute('position', new THREE.BufferAttribute(particlePositions, 3));

    function createParticleTexture() {
        const canvas = document.createElement('canvas');
        canvas.width = 16;
        canvas.height = 16;
        const ctx = canvas.getContext('2d');
        const grad = ctx.createRadialGradient(8, 8, 0, 8, 8, 8);
        grad.addColorStop(0, 'rgba(56, 189, 248, 1)');
        grad.addColorStop(0.4, 'rgba(0, 240, 255, 0.6)');
        grad.addColorStop(1, 'rgba(0, 0, 0, 0)');
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(8, 8, 8, 0, Math.PI * 2);
        ctx.fill();
        return new THREE.CanvasTexture(canvas);
    }

    const particleMat = new THREE.PointsMaterial({
        size: 0.085,
        map: createParticleTexture(),
        transparent: true,
        opacity: 0.85,
        blending: THREE.AdditiveBlending,
        depthWrite: false
    });
    const particles = new THREE.Points(particleGeo, particleMat);
    scene.add(particles);

    // 7. GLTF Model Loader & Material Enhancement
    let model;
    let mixer;
    let autoOrbit = false;
    const pointer = new THREE.Vector2();
    const targetRotation = new THREE.Vector2(-0.02, -0.35);
    const drag = { active: false, x: 0, y: 0 };
    const loader = new THREE.GLTFLoader();

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

        // Scale model to a comfortable height (1.55 units total height)
        const initialBox = new THREE.Box3().setFromObject(model);
        const initialSize = initialBox.getSize(new THREE.Vector3());
        const fitScale = 1.55 / Math.max(initialSize.x, initialSize.y, initialSize.z, 0.01);
        model.scale.setScalar(fitScale);

        // Align model so bottom of boots rests EXACTLY at y = 0 on top of the hologram pedestal!
        const box = new THREE.Box3().setFromObject(model);
        const center = box.getCenter(new THREE.Vector3());
        model.position.x = -center.x;
        model.position.z = -center.z;
        model.position.y = -box.min.y; // Boots sit directly on the pedestal plane y = 0!

        // Recolor armor to Weathered Battle-Worn Arctic White Ceramic + Icy Cyan Visor Glow
        model.traverse(function (child) {
            if (!child.isMesh) return;
            child.frustumCulled = false;
            if (child.material) {
                const materials = Array.isArray(child.material) ? child.material : [child.material];
                materials.forEach(function (mat) {
                    mat.side = THREE.DoubleSide;
                    mat.transparent = false;
                    mat.depthWrite = true;

                    const matName = (mat.name || '').toLowerCase();
                    const meshName = (child.name || '').toLowerCase();

                    // Check if material/mesh is the Visor/Helmet Face Accents
                    if (matName.includes('visor') || meshName.includes('visor') || matName.includes('glass')) {
                        mat.color.setHex(0x38bdf8); // Glowing Icy Cyan Visor
                        if (mat.emissive) mat.emissive.setHex(0x0284c7);
                        if (mat.roughness !== undefined) mat.roughness = 0.1;
                        if (mat.metalness !== undefined) mat.metalness = 0.95;
                    } else {
                        // Weathered Arctic Bone-White Ceramic Armor
                        mat.color.setHex(0xe2e8f0);
                        if (mat.roughness !== undefined) mat.roughness = 0.52; // Matte weathered paint finish
                        if (mat.metalness !== undefined) mat.metalness = 0.45; // Battle-worn ceramic composite

                        // Boost normal map scratch depth so surface scratches & battle dents pop out aggressively
                        if (mat.normalScale) {
                            mat.normalScale.set(2.2, 2.2);
                        }
                    }

                    mat.needsUpdate = true;
                });
            }
        });

        characterGroup.add(model);

        if (gltf.animations && gltf.animations.length) {
            mixer = new THREE.AnimationMixer(model);
            mixer.clipAction(gltf.animations[0]).setLoop(THREE.LoopRepeat).play();
        }

        if (loading) loading.style.display = 'none';
        console.info('3D asset loaded:', source);
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

    // 8. Interaction Handlers & Raycasting Mesh Selection
    const raycaster = new THREE.Raycaster();
    let clickTracker = { x: 0, y: 0, time: 0 };

    host.addEventListener('pointermove', function (event) {
        const rect = host.getBoundingClientRect();
        pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
        pointer.y = -(((event.clientY - rect.top) / rect.height) * 2 - 1);

        // Hover Raycast: Show pointer cursor ONLY when hovering directly over Master Chief's 3D mesh
        if (!drag.active && model) {
            raycaster.setFromCamera(pointer, camera);
            const intersects = raycaster.intersectObject(model, true);
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
        clickTracker.x = event.clientX;
        clickTracker.y = event.clientY;
        clickTracker.time = Date.now();
        host.setPointerCapture(event.pointerId);
    });

    host.addEventListener('pointerup', function (event) {
        drag.active = false;
        try { host.releasePointerCapture(event.pointerId); } catch (e) {}

        const dist = Math.hypot(event.clientX - clickTracker.x, event.clientY - clickTracker.y);
        const duration = Date.now() - clickTracker.time;

        // Precision Click: Navigate to home ONLY if the user clicked directly on Master Chief's 3D mesh
        if (dist < 8 && duration < 350 && model) {
            const rect = host.getBoundingClientRect();
            const clickMouse = new THREE.Vector2(
                ((event.clientX - rect.left) / rect.width) * 2 - 1,
                -(((event.clientY - rect.top) / rect.height) * 2 - 1)
            );

            raycaster.setFromCamera(clickMouse, camera);
            const intersects = raycaster.intersectObject(model, true);

            if (intersects && intersects.length > 0) {
                console.info('Direct 3D Master Chief Mesh Clicked — Navigating to Landing Page...');
                window.location.href = '/';
            }
        }
    });

    // Global HUD controls expose
    window.authSceneControls = {
        resetView: function () {
            targetRotation.set(-0.02, -0.35);
            autoOrbit = false;
        },
        toggleOrbit: function () {
            autoOrbit = !autoOrbit;
            return autoOrbit;
        }
    };

    function resize() {
        const rect = host.getBoundingClientRect();
        camera.aspect = Math.max(rect.width, 1) / Math.max(rect.height, 1);
        camera.updateProjectionMatrix();
        renderer.setSize(Math.max(rect.width, 1), Math.max(rect.height, 1), false);
    }
    window.addEventListener('resize', resize);
    resize();

    // 9. Animation Loop
    const clock = new THREE.Clock();
    function animate() {
        requestAnimationFrame(animate);
        const dt = clock.getDelta();

        if (mixer) mixer.update(dt);

        // Rotate Holographic Pedestal
        ring1.rotation.z += 0.006;
        ring2.rotation.z -= 0.009;

        // Floating Data Particles Animation
        const positions = particleGeo.attributes.position.array;
        for (let i = 0; i < particleCount; i++) {
            positions[i * 3 + 1] += particleVelocities[i];
            if (positions[i * 3 + 1] > 1.9) {
                positions[i * 3 + 1] = -0.85;
                positions[i * 3] = (Math.random() - 0.5) * 4.0;
            }
        }
        particleGeo.attributes.position.needsUpdate = true;

        // Smooth Sway & Position Adjustment
        if (characterGroup) {
            if (autoOrbit && !drag.active) {
                targetRotation.y += dt * 0.5;
            }

            // Desktop layout positioning:
            // Position pedestal base at y = -0.82 so entire Master Chief character fits in full view (head at y = +0.73, well below top edge!)
            const isDesktop = window.innerWidth > 960;
            const targetX = isDesktop ? 0.42 : 0;
            const targetY = isDesktop ? -0.82 : -0.78;

            characterGroup.position.x += (targetX - characterGroup.position.x) * 0.08;
            characterGroup.position.y += (targetY + Math.sin(clock.getElapsedTime() * 1.5) * 0.01 - characterGroup.position.y) * 0.08;
            characterGroup.position.z = 0;

            characterGroup.rotation.y += (targetRotation.y - characterGroup.rotation.y) * 0.08;
            characterGroup.rotation.x += (targetRotation.x - characterGroup.rotation.x) * 0.08;
        }

        renderer.render(scene, camera);
    }
    animate();
}());