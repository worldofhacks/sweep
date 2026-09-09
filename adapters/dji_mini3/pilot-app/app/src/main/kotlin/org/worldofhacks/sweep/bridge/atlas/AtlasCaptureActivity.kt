package org.worldofhacks.sweep.bridge.atlas

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.video.FileOutputOptions
import androidx.camera.video.Quality
import androidx.camera.video.QualitySelector
import androidx.camera.video.Recording
import androidx.camera.video.VideoRecordEvent
import androidx.camera.view.CameraController
import androidx.camera.view.LifecycleCameraController
import androidx.camera.view.PreviewView
import androidx.camera.view.video.AudioConfig
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import org.json.JSONObject
import org.worldofhacks.sweep.bridge.ui.SweepPalette
import org.worldofhacks.sweep.bridge.ui.SweepTheme
import java.util.concurrent.Executors

class AtlasCaptureActivity : ComponentActivity() {
    private lateinit var camera: LifecycleCameraController
    private lateinit var sensors: AtlasSensors
    private lateinit var session: AtlasSession
    private var captureRequest: AtlasCaptureRequest? = null
    private val queue by lazy { AtlasOutbox.get(this) }
    private var cameraAllowed by mutableStateOf(false)
    private var locationAllowed by mutableStateOf(false)
    private var mode by mutableStateOf("photo")
    private var saving by mutableStateOf(false)
    private var recording by mutableStateOf(false)
    private var count by mutableIntStateOf(0)
    private var elapsed by mutableLongStateOf(0L)
    private var locationLabel by mutableStateOf("Location optional · no map coverage without GPS")
    private var message by mutableStateOf("Your originals are saved on this phone before uploading.")
    private var activeRecording: Recording? = null
    private val cameraPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        cameraAllowed = granted
        if (cameraAllowed) bindCamera()
    }
    private val locationPermission = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        locationAllowed = allowed(Manifest.permission.ACCESS_COARSE_LOCATION)
        // Location is independent of capture; a permission reply must not rebind the camera.
        sensors.start()
    }
    private fun allowed(permission: String) = ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        session = AtlasVault(this).load(intent.getStringExtra("session").orEmpty()) ?: run { finish(); return }
        runCatching { session.endpoint(intent.getStringExtra("space").orEmpty()) }.getOrElse { finish(); return }
        captureRequest = runCatching { intent.getStringExtra("request")?.let { AtlasCaptureRequest.parse(JSONObject(it)) } }
            .getOrElse { finish(); return }
        sensors = AtlasSensors(this) { locationLabel = it }
        camera = LifecycleCameraController(this).apply {
            setEnabledUseCases(CameraController.IMAGE_CAPTURE)
            videoCaptureQualitySelector = QualitySelector.fromOrderedList(listOf(Quality.HD, Quality.SD, Quality.FHD))
            imageCaptureMode = ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY
        }
        cameraAllowed = allowed(Manifest.permission.CAMERA)
        locationAllowed = allowed(Manifest.permission.ACCESS_COARSE_LOCATION)
        enableEdgeToEdge()
        setContent {
            SweepTheme {
                Column(Modifier.fillMaxSize().background(Mineral).safeDrawingPadding().padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text("ADD YOUR PERSPECTIVE", color = Pine, fontSize = 11.sp, letterSpacing = 1.sp, fontWeight = FontWeight.Bold)
                            Text(intent.getStringExtra("title").orEmpty().ifBlank { "Space capture" }, fontSize = 22.sp,
                                fontWeight = FontWeight.SemiBold, maxLines = 2, color = Ink)
                        }
                        TextButton(onClick = { activeRecording?.stop(); finish() }) { Text("Done") }
                    }
                    Box(Modifier.weight(1f).fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(Ink), contentAlignment = Alignment.Center) {
                        if (cameraAllowed) AndroidView(factory = { PreviewView(it).apply {
                            implementationMode = PreviewView.ImplementationMode.COMPATIBLE
                            controller = camera
                        } }, modifier = Modifier.fillMaxSize())
                        else Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
                            Text("See the place.\nAdd your perspective.", color = Color.White, fontSize = 24.sp)
                            Text("Allow the camera to take a photo or silent video. Location is optional and is recorded only with your permission.", color = Color.White)
                            Button(onClick = ::requestCamera) { Text("Enable camera") }
                        }
                        if (recording) Text("RECORDING · ${elapsed}s / 60s", color = Color.White,
                            modifier = Modifier.align(Alignment.TopCenter).padding(16.dp).background(Ink, RoundedCornerShape(8.dp)).padding(8.dp))
                    }
                    Column(Modifier.heightIn(max = 320.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        captureRequest?.let { request ->
                            Text("REQUESTED VIEW · ${request.label}", color = Pine, fontSize = 12.sp, fontWeight = FontWeight.Bold)
                            Text(request.note, fontSize = 14.sp, color = Ink)
                            Text("Contribute only where safe and permitted. Uploads do not certify coverage or a safe route.", fontSize = 12.sp, color = Ink)
                        }
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            listOf("photo" to "Photo", "video" to "Video", "panorama" to "360 scan").forEach { (value, label) ->
                                FilterChip(selected = mode == value, enabled = !saving && !recording,
                                    onClick = { mode = value; bindCamera() }, label = { Text(label) }, modifier = Modifier.weight(1f).heightIn(min = 48.dp))
                            }
                        }
                        Text(when (mode) {
                            "panorama" -> "View ${count % 8 + 1} of 8 · Walk safely around your subject. Keep half of the previous view in frame; rotating in one spot cannot recover depth."
                            "video" -> "Walk slowly with overlapping views. Silent video, up to 60 seconds. Keep people and traffic at a safe distance."
                            else -> "Capture from different viewpoints, with plenty of overlap. Never enter a restricted or unsafe area."
                        }, fontSize = 14.sp, color = Ink)
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(locationLabel, fontSize = 12.sp, color = Pine, modifier = Modifier.weight(1f))
                            if (!locationAllowed) TextButton(onClick = { locationPermission.launch(arrayOf(
                                Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)) }) {
                                Text("Enable location", fontSize = 12.sp)
                            }
                        }
                        Button(onClick = { if (recording) activeRecording?.stop() else capture() },
                            enabled = cameraAllowed && !saving, colors = ButtonDefaults.buttonColors(containerColor = Pine),
                            modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp), shape = RoundedCornerShape(12.dp)) {
                            Text(if (saving) "Saving on device…" else if (recording) "Stop & save video" else if (mode == "video") "Start video" else "Take ${if (mode == "panorama") "view ${count % 8 + 1}" else "photo"}", fontSize = 16.sp)
                        }
                        Text(message, fontSize = 12.sp, color = Ink, modifier = Modifier.fillMaxWidth()
                            .border(1.dp, SweepPalette.Line, RoundedCornerShape(8.dp)).padding(8.dp))
                    }
                }
            }
        }
        if (cameraAllowed) bindCamera() else requestCamera()
    }
    private fun requestCamera() = cameraPermission.launch(Manifest.permission.CAMERA)
    private fun bindCamera() {
        if (!cameraAllowed) return
        try {
            camera.setEnabledUseCases(if (mode == "video") CameraController.VIDEO_CAPTURE else CameraController.IMAGE_CAPTURE)
            camera.bindToLifecycle(this)
        } catch (_: Exception) { message = "The camera cannot open in this mode. Try Photo or another camera app." }
    }
    private fun metadata(): JSONObject = JSONObject().put("contributor_id", intent.getStringExtra("contributor"))
        .put("name", intent.getStringExtra("name").orEmpty().ifBlank { "Contributor" }).put("kind", mode)
        .put("source", "camera").put("captured_at", System.currentTimeMillis())
        .put("position", sensors.position() ?: JSONObject.NULL).put("note", "")
        .put("response_to", captureRequest?.target())

    private fun capture() {
        if (saving || recording) return
        val item = try { queue.begin(session, intent.getStringExtra("space").orEmpty(), metadata()) }
            catch (error: Exception) { message = error.message ?: "The capture queue is unavailable."; return }
        val main = ContextCompat.getMainExecutor(this)
        try {
            if (mode == "video") {
                saving = true
                val options = FileOutputOptions.Builder(queue.file(item)).setFileSizeLimit(60L * 1024 * 1024)
                    .setDurationLimitMillis(60_000).build()
                activeRecording = camera.startRecording(options, AudioConfig.AUDIO_DISABLED, main) { event ->
                    when (event) {
                        is VideoRecordEvent.Start -> { queue.captureMetadata(item.id, metadata()); recording = true; saving = false; elapsed = 0 }
                        is VideoRecordEvent.Status -> { elapsed = event.recordingStats.recordedDurationNanos / 1_000_000_000 }
                        is VideoRecordEvent.Finalize -> {
                            recording = false; saving = true; activeRecording = null
                            if (!event.hasError() || event.error in setOf(VideoRecordEvent.Finalize.ERROR_DURATION_LIMIT_REACHED,
                                    VideoRecordEvent.Finalize.ERROR_FILE_SIZE_LIMIT_REACHED, VideoRecordEvent.Finalize.ERROR_SOURCE_INACTIVE)) finalize(item)
                            else failed(item, "Video could not be finalized. The local file is retained; take another capture.")
                        }
                    }
                }
            } else {
                saving = true
                camera.takePicture(ImageCapture.OutputFileOptions.Builder(queue.file(item)).build(), main,
                    object : ImageCapture.OnImageSavedCallback {
                        override fun onCaptureStarted() { queue.captureMetadata(item.id, metadata()) }
                        override fun onImageSaved(result: ImageCapture.OutputFileResults) { finalize(item) }
                        override fun onError(exception: ImageCaptureException) { failed(item, "Photo capture failed. Try again; any partial file is retained.") }
                    })
            }
        } catch (_: Exception) { failed(item, "The camera could not capture this view. Try again.") }
    }
    private fun finalize(item: AtlasUpload) {
        val context = applicationContext
        val outbox = queue
        // Outlives the Activity, so pressing Done after the shutter does not discard the file.
        FILES.execute {
            try {
                outbox.finish(item.id)
                AtlasUploadWorker.enqueue(context, item.id)
                runOnUiThread { saving = false; count++; message = "$count captured · Saved on device. Uploads continue when connected." }
            } catch (error: Exception) { runOnUiThread { failed(item, error.message ?: "Capture could not be finalized.") } }
        }
    }
    private fun failed(item: AtlasUpload, reason: String) { queue.state(item.id, "failed", reason); saving = false; recording = false; message = reason }
    override fun onStart() { super.onStart(); if (::sensors.isInitialized) sensors.start() }
    override fun onStop() { activeRecording?.stop(); if (::sensors.isInitialized) sensors.stop(); super.onStop() }
    override fun onDestroy() { if (::camera.isInitialized) camera.unbind(); super.onDestroy() }
    companion object {
        private val FILES = Executors.newSingleThreadExecutor()
        private val Pine = SweepPalette.Pine
        private val Ink = SweepPalette.Ink
        private val Mineral = SweepPalette.Mineral
    }
}
