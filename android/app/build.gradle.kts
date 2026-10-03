import java.io.File

plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

val repoRoot: File = rootProject.projectDir.parentFile
val pySrc = layout.buildDirectory.dir("pysrc")

android {
    namespace = "com.scalping.terminal"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.scalping.terminal"
        minSdk = 24
        targetSdk = 35
        versionCode = 2
        versionName = "0.2.0"
        ndk {
            // 64-bit ARM covers practically every phone from the last ~8 years and keeps the APK small.
            // Add "x86_64" for emulators.
            abiFilters += listOf("arm64-v8a")
        }
    }

    signingConfigs {
        create("release") {
            // A committed, *public* development key so every build can update the previous one.
            // For store distribution, pass your own key via SCALPER_KEYSTORE / SCALPER_KEYSTORE_PASSWORD /
            // SCALPER_KEY_ALIAS / SCALPER_KEY_PASSWORD.
            val ks = System.getenv("SCALPER_KEYSTORE")
            storeFile = if (ks != null) file(ks) else file("dev-release.keystore")
            storePassword = System.getenv("SCALPER_KEYSTORE_PASSWORD") ?: "scalping-dev"
            keyAlias = System.getenv("SCALPER_KEY_ALIAS") ?: "scalping"
            keyPassword = System.getenv("SCALPER_KEY_PASSWORD") ?: "scalping-dev"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    packaging {
        resources.excludes += listOf("META-INF/*.version", "META-INF/LICENSE*", "kotlin/**")
    }
}

chaquopy {
    defaultConfig {
        version = "3.11"
        System.getenv("SCALPER_BUILD_PYTHON")?.let { buildPython(it) }
        pip {
            install("-r", "requirements-mobile.txt")
        }
        pyc {
            src = true      // ship compiled .pyc only: smaller and faster to start
        }
        // Packages that must exist as real files on the device (served/read by path).
        extractPackages("scalper_webui", "scalper_conf")
    }
    sourceSets {
        getByName("main") {
            srcDir(pySrc)
        }
    }
}

// Stage the Python engine, the built web UI and the config into one Python source tree.
val stagePython by tasks.registering(Sync::class) {
    val dist = File(repoRoot, "frontend/dist")
    doFirst {
        if (!File(dist, "index.html").exists()) {
            throw GradleException("frontend/dist is missing — run `npm ci && npm run build` in frontend/ first")
        }
    }
    into(pySrc)
    from(File(repoRoot, "backend/scalper")) {
        into("scalper")
        exclude("**/__pycache__/**", "**/*.pyc")
    }
    from(dist) { into("scalper_webui") }
    from(File(repoRoot, "config")) {
        into("scalper_conf")
        include("default.yaml", "mobile.yaml")
    }
    doLast {
        File(pySrc.get().asFile, "scalper_webui/__init__.py").writeText("")
        File(pySrc.get().asFile, "scalper_conf/__init__.py").writeText("")
    }
}

tasks.named("preBuild") { dependsOn(stagePython) }
tasks.matching { it.name.contains("Python") && it.name != "stagePython" }.configureEach { dependsOn(stagePython) }
