package org.worldofhacks.sweep.bridge.camera

import dji.sdk.keyvalue.key.CameraKey
import dji.sdk.keyvalue.key.DJIKey
import dji.sdk.keyvalue.key.GimbalKey
import dji.sdk.keyvalue.key.KeyTools
import dji.sdk.keyvalue.value.camera.CameraFlatMode
import dji.sdk.keyvalue.value.camera.CameraMode
import dji.sdk.keyvalue.value.camera.CameraStorageInfos
import dji.sdk.keyvalue.value.camera.CameraStorageLocation
import dji.sdk.keyvalue.value.camera.GeneratedMediaFileInfo
import dji.sdk.keyvalue.value.camera.MediaFileType
import dji.sdk.keyvalue.value.camera.PhotoPanoramaMode
import dji.sdk.keyvalue.value.camera.PhotoRatio
import dji.sdk.keyvalue.value.camera.SDCardLoadState
import dji.sdk.keyvalue.value.common.Attitude
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.sdk.keyvalue.value.common.EmptyMsg
import dji.sdk.keyvalue.value.gimbal.GimbalAngleRotation
import dji.sdk.keyvalue.value.gimbal.GimbalAngleRotationMode
import dji.sdk.keyvalue.value.gimbal.GimbalAttitudeRange
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.manager.KeyManager
import dji.v5.manager.datacenter.MediaDataCenter
import dji.v5.manager.datacenter.media.MediaFile
import dji.v5.manager.datacenter.media.MediaFileDownloadListener
import dji.v5.manager.datacenter.media.MediaFileFilter
import dji.v5.manager.datacenter.media.MediaFileListDataSource
import dji.v5.manager.datacenter.media.PullMediaFileListParam
import java.io.File
import java.io.FileOutputStream
import java.util.Calendar
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import org.worldofhacks.sweep.bridge.core.flight.PortResult

/**
 * The probe flavor's [CameraPort] on MSDK 5.18.0: camera and storage facts from
 * `KeyManager` listeners (`KeyConnection`, `KeyCameraFlatMode` / `KeyCameraMode`,
 * `KeyCameraStorageInfos`, `KeyCameraStorageLocation`, `KeyPhotoRatio`,
 * `KeyVisionPhotoPanoramaModeRange`), the gimbal through `KeyGimbalAttitude`,
 * `KeyGimbalAttitudeRange`, and the `KeyRotateByAngle` action (absolute pitch, roll and yaw
 * ignored), the shutter through `KeyStartShootPhoto` with `KeyNewlyGeneratedMediaFile` as
 * the announcement of the new file, and retrieval through `IMediaManager`: `enable`, the
 * file list pulled from the current storage, `pullOriginalMediaFileFromCamera` for the
 * file whose index the camera announced, then `disable` so the camera can shoot again.
 *
 * Like the telemetry listeners, every key is listened as soon as the SDK registers,
 * whatever `isKeySupported` says at that moment; a key the product really lacks never
 * fires and the corresponding fact stays null.
 */
class DjiCameraPort(private val log: (name: String, detail: String) -> Unit) : CameraPort {
    private val holder = Any()
    private val lock = Any()
    private val _facts = MutableStateFlow(CameraFacts())
    override val facts: StateFlow<CameraFacts> = _facts.asStateFlow()

    private var attached = false
    private var listener: ((CameraFile) -> Unit)? = null

    @Volatile
    private var gimbalPitch: Double? = null

    @Volatile
    private var flatModeSupported: Boolean? = null

    @Volatile
    private var storageLocation: CameraStorageLocation? = null

    @Volatile
    private var mediaEnabled = false

    private val manager
        get() = MediaDataCenter.getInstance().mediaManager

    /** SdkSession hook: after registration and on every product connect; safe to call again. */
    fun attach() {
        synchronized(lock) {
            if (attached) return
            attached = true
        }
        listen(KeyTools.createKey(CameraKey.KeyConnection, CAMERA)) { connected ->
            _facts.update { it.copy(cameraConnected = connected) }
            log("Camera", if (connected) "camera connected" else "camera disconnected")
        }
        listen(KeyTools.createKey(CameraKey.KeyCameraFlatModeSupported, CAMERA)) { supported -> flatModeSupported = supported }
        listen(KeyTools.createKey(CameraKey.KeyCameraFlatMode, CAMERA)) { mode ->
            if (flatModeSupported != false) _facts.update { it.copy(photoMode = mode == CameraFlatMode.PHOTO_NORMAL) }
        }
        listen(KeyTools.createKey(CameraKey.KeyCameraMode, CAMERA)) { mode ->
            if (flatModeSupported == false) _facts.update { it.copy(photoMode = mode == CameraMode.PHOTO_NORMAL) }
        }
        listen(KeyTools.createKey(CameraKey.KeyCameraStorageLocation, CAMERA)) { location -> storageLocation = location }
        listen(KeyTools.createKey(CameraKey.KeyCameraStorageInfos, CAMERA)) { infos -> applyStorage(infos) }
        listen(KeyTools.createKey(CameraKey.KeyPhotoRatio, CAMERA)) { ratio ->
            val (width, height) = photoSize(ratio)
            _facts.update { it.copy(photoWidthPx = width, photoHeightPx = height) }
        }
        listen(KeyTools.createKey(CameraKey.KeyVisionPhotoPanoramaModeRange, CAMERA)) { modes ->
            val advertised = modes.filter { it != PhotoPanoramaMode.MODE_NONE && it != PhotoPanoramaMode.UNKNOWN }.map { it.name }
            _facts.update { it.copy(panoramaAdvertised = advertised) }
        }
        listen(KeyTools.createKey(CameraKey.KeyNewlyGeneratedMediaFile, CAMERA)) { info -> announce(info) }
        listen(KeyTools.createKey(GimbalKey.KeyGimbalAttitude)) { attitude: Attitude -> gimbalPitch = attitude.pitch }
        listen(KeyTools.createKey(GimbalKey.KeyGimbalAttitudeRange)) { range: GimbalAttitudeRange ->
            val pitch = range.pitch
            _facts.update { it.copy(gimbalPitchMinDeg = pitch?.min, gimbalPitchMaxDeg = pitch?.max) }
        }
        log("Camera keys", "camera, storage, gimbal, and media-file listeners registered")
    }

    fun detach() {
        synchronized(lock) {
            if (!attached) return
            attached = false
        }
        KeyManager.getInstance().cancelListen(holder)
    }

    /** SdkSession hook: the SDK manager's product connect and disconnect callbacks. */
    fun productConnected(connected: Boolean) {
        if (!connected) {
            _facts.update { it.copy(cameraConnected = false, photoMode = false) }
            gimbalPitch = null
            mediaEnabled = false
        }
    }

    override fun refreshFacts(onResult: (PortResult) -> Unit) {
        val keyManager = KeyManager.getInstance()
        read(keyManager, KeyTools.createKey(CameraKey.KeyCameraFlatModeSupported, CAMERA)) { flatModeSupported = it }
        read(keyManager, KeyTools.createKey(GimbalKey.KeyGimbalAttitudeRange)) { range ->
            _facts.update { it.copy(gimbalPitchMinDeg = range.pitch?.min, gimbalPitchMaxDeg = range.pitch?.max) }
        }
        read(keyManager, KeyTools.createKey(CameraKey.KeyVisionPhotoPanoramaModeRange, CAMERA)) { modes ->
            _facts.update { it.copy(panoramaAdvertised = modes.filter { m -> m != PhotoPanoramaMode.MODE_NONE && m != PhotoPanoramaMode.UNKNOWN }.map { m -> m.name }) }
        }
        val storageKey = KeyTools.createKey(CameraKey.KeyCameraStorageInfos, CAMERA)
        if (!keyManager.isKeySupported(storageKey)) {
            onResult(PortResult.Failed("KeyCameraStorageInfos is not supported by the connected product"))
            return
        }
        keyManager.getValue(
            storageKey,
            object : CommonCallbacks.CompletionCallbackWithParam<CameraStorageInfos> {
                override fun onSuccess(value: CameraStorageInfos?) {
                    if (value != null) applyStorage(value)
                    onResult(PortResult.Ok)
                }

                override fun onFailure(error: IDJIError) = onResult(PortResult.Failed("storage read failed: ${describe(error)}"))
            },
        )
    }

    override fun gimbalPitchDeg(): Double? = gimbalPitch

    override fun setGimbalPitch(pitchDeg: Double, onResult: (PortResult) -> Unit) {
        val rotation = GimbalAngleRotation(
            GimbalAngleRotationMode.ABSOLUTE_ANGLE,
            pitchDeg,
            0.0,
            0.0,
            false,
            true,
            true,
            ROTATION_DURATION_S,
            false,
            0,
        )
        performAction(KeyTools.createKey(GimbalKey.KeyRotateByAngle), rotation, onResult)
    }

    override fun enterPhotoMode(onResult: (PortResult) -> Unit) {
        if (!mediaEnabled) {
            setPhotoMode(onResult)
            return
        }
        // The media manager puts the camera in playback; release it before the shutter.
        manager.disable(
            object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    mediaEnabled = false
                    setPhotoMode(onResult)
                }

                override fun onFailure(error: IDJIError) {
                    log("Media manager", "disable failed: ${describe(error)}; setting photo mode anyway")
                    mediaEnabled = false
                    setPhotoMode(onResult)
                }
            },
        )
    }

    override fun shootPhoto(onResult: (PortResult) -> Unit) {
        val key = KeyTools.createKey(CameraKey.KeyStartShootPhoto, CAMERA)
        val keyManager = KeyManager.getInstance()
        if (!keyManager.isKeySupported(key)) {
            onResult(PortResult.Failed("KeyStartShootPhoto is not supported by the connected product"))
            return
        }
        keyManager.performAction(
            key,
            object : CommonCallbacks.CompletionCallbackWithParam<EmptyMsg> {
                override fun onSuccess(value: EmptyMsg?) = onResult(PortResult.Ok)

                override fun onFailure(error: IDJIError) = onResult(PortResult.Failed(describe(error)))
            },
        )
    }

    override fun setFileListener(listener: ((CameraFile) -> Unit)?) {
        synchronized(lock) { this.listener = listener }
    }

    override fun download(file: CameraFile, target: File, listener: DownloadListener) {
        val mediaManager = manager
        mediaManager.enable(
            object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    mediaEnabled = true
                    pullList(file, target, listener)
                }

                override fun onFailure(error: IDJIError) = listener.failed("media manager enable failed: ${describe(error)}")
            },
        )
    }

    override fun leaveMediaMode(onResult: (PortResult) -> Unit) {
        if (!mediaEnabled) {
            onResult(PortResult.Ok)
            return
        }
        manager.disable(
            object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    mediaEnabled = false
                    onResult(PortResult.Ok)
                }

                override fun onFailure(error: IDJIError) {
                    mediaEnabled = false
                    onResult(PortResult.Failed("media manager disable failed: ${describe(error)}"))
                }
            },
        )
    }

    // ---- internals ----

    private fun setPhotoMode(onResult: (PortResult) -> Unit) {
        val keyManager = KeyManager.getInstance()
        val done = object : CommonCallbacks.CompletionCallback {
            override fun onSuccess() {
                _facts.update { it.copy(photoMode = true) }
                onResult(PortResult.Ok)
            }

            override fun onFailure(error: IDJIError) = onResult(PortResult.Failed("photo mode refused: ${describe(error)}"))
        }
        if (flatModeSupported == false) {
            keyManager.setValue(KeyTools.createKey(CameraKey.KeyCameraMode, CAMERA), CameraMode.PHOTO_NORMAL, done)
        } else {
            keyManager.setValue(KeyTools.createKey(CameraKey.KeyCameraFlatMode, CAMERA), CameraFlatMode.PHOTO_NORMAL, done)
        }
    }

    private fun pullList(file: CameraFile, target: File, listener: DownloadListener) {
        val mediaManager = manager
        val location = storageLocation ?: CameraStorageLocation.SDCARD
        mediaManager.setMediaFileDataSource(MediaFileListDataSource.Builder().setLocation(location).setIndexType(CAMERA).build())
        val param = PullMediaFileListParam.Builder().mediaFileIndex(ALL_FILES).count(ALL_FILES).filter(MediaFileFilter.PHOTO).build()
        mediaManager.pullMediaFileListFromCamera(
            param,
            object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    val files = mediaManager.mediaFileListData?.data.orEmpty()
                    val match = files.firstOrNull { it.fileIndex == file.index }
                    if (match == null) {
                        listener.failed("file index ${file.index} (${file.name}) is not in the camera's list of ${files.size} photos on $location")
                        return
                    }
                    log("Media manager", "downloading ${match.fileName} (${match.fileSize} bytes, index ${match.fileIndex}) to ${target.absolutePath}")
                    pullFile(match, target, listener)
                }

                override fun onFailure(error: IDJIError) = listener.failed("media file list failed: ${describe(error)}")
            },
        )
    }

    /**
     * The SDK streams the file in chunks and calls `onFinish` whatever happened to them on
     * the phone, so a write error (disk full, I/O) and the byte count decide the outcome
     * here: a short or unwritten file is removed and reported as a failure, never handed
     * to the executor to checksum as a completed capture.
     */
    private fun pullFile(media: MediaFile, target: File, listener: DownloadListener) {
        target.parentFile?.mkdirs()
        val stream = try {
            FileOutputStream(target)
        } catch (error: java.io.IOException) {
            listener.failed("cannot open ${target.absolutePath}: ${error.message}")
            return
        }
        media.pullOriginalMediaFileFromCamera(
            0L,
            object : MediaFileDownloadListener {
                @Volatile
                private var writeError: java.io.IOException? = null

                override fun onStart() = listener.progress(0, media.fileSize)

                override fun onProgress(total: Long, current: Long) = listener.progress(current, total)

                override fun onRealtimeDataUpdate(data: ByteArray, position: Long) {
                    if (writeError != null) return
                    try {
                        stream.write(data)
                    } catch (error: java.io.IOException) {
                        writeError = error
                        log("Media manager", "write failed at $position: ${error.message}")
                    }
                }

                override fun onFinish() {
                    try {
                        stream.close()
                    } catch (error: java.io.IOException) {
                        if (writeError == null) writeError = error
                    }
                    val expected = media.fileSize
                    val written = target.length()
                    val error = writeError
                    val problem = when {
                        error != null -> "download truncated: $written of $expected bytes: ${error.message}"
                        expected <= 0 -> "download unverified: the camera listed no size for ${media.fileName}; $written bytes written"
                        written != expected -> "download truncated: $written of $expected bytes"
                        else -> null
                    }
                    if (problem != null) {
                        target.delete()
                        listener.failed("$problem; partial file removed")
                        return
                    }
                    listener.finished()
                }

                override fun onFailure(error: IDJIError) {
                    runCatching { stream.close() }
                    target.delete()
                    listener.failed("download failed: ${describe(error)}; partial file removed")
                }
            },
        )
    }

    private fun applyStorage(infos: CameraStorageInfos) {
        val current = infos.currentCameraStorageInfo ?: infos.cameraStorageInfoList?.firstOrNull()
        storageLocation = infos.currentStorageType ?: storageLocation
        val leftMb = current?.storageLeftCapacity
        _facts.update {
            it.copy(
                storageInserted = current?.storageState == SDCardLoadState.INSERTED,
                storageRemainingBytes = leftMb?.toLong()?.times(BYTES_PER_MB),
            )
        }
    }

    private fun announce(info: GeneratedMediaFileInfo) {
        val index = info.index ?: return
        val extension = when (info.type) {
            MediaFileType.JPEG -> "JPG"
            MediaFileType.DNG -> "DNG"
            null -> "BIN"
            else -> info.type.name
        }
        val name = "DJI_%04d.%s".format(info.file_no ?: index, extension)
        val created = info.createTime?.let { time ->
            runCatching {
                Calendar.getInstance().apply {
                    set(time.year ?: 1970, (time.month ?: 1) - 1, time.day ?: 1, time.hour ?: 0, time.minute ?: 0, time.second ?: 0)
                }.timeInMillis
            }.getOrNull()
        }
        val file = CameraFile(index = index, name = name, sizeBytes = info.fileSize?.toLong() ?: 0L, createdAtMs = created)
        log("Camera", "new file announced: $name index $index, ${file.sizeBytes} bytes")
        synchronized(lock) { listener }?.invoke(file)
    }

    private fun photoSize(ratio: PhotoRatio): Pair<Int, Int> = when (ratio) {
        PhotoRatio.RATIO_16COLON9 -> 4000 to 2250
        PhotoRatio.RATIO_3COLON2 -> 4000 to 2667
        PhotoRatio.RATIO_SQUARE -> 3000 to 3000
        else -> 4000 to 3000
    }

    private fun <T : Any> performAction(key: DJIKey.ActionKey<T, EmptyMsg>, parameter: T, onResult: (PortResult) -> Unit) {
        val keyManager = KeyManager.getInstance()
        if (!keyManager.isKeySupported(key)) {
            onResult(PortResult.Failed("${key.keyInfo.identifier} is not supported by the connected product"))
            return
        }
        keyManager.performAction(
            key,
            parameter,
            object : CommonCallbacks.CompletionCallbackWithParam<EmptyMsg> {
                override fun onSuccess(value: EmptyMsg?) = onResult(PortResult.Ok)

                override fun onFailure(error: IDJIError) = onResult(PortResult.Failed(describe(error)))
            },
        )
    }

    private fun <T : Any> read(keyManager: KeyManager, key: DJIKey<T>, apply: (T) -> Unit) {
        if (!keyManager.isKeySupported(key)) return
        keyManager.getValue(
            key,
            object : CommonCallbacks.CompletionCallbackWithParam<T> {
                override fun onSuccess(value: T?) {
                    if (value != null) apply(value)
                }

                override fun onFailure(error: IDJIError) = log("Camera", "${key.keyInfo.identifier} read failed: ${describe(error)}")
            },
        )
    }

    /** Listeners are registered without a support check: the product may connect after registration. */
    private fun <T : Any> listen(key: DJIKey<T>, apply: (T) -> Unit) {
        KeyManager.getInstance().listen(key, holder, CommonCallbacks.KeyListener<T> { _, newValue -> if (newValue != null) apply(newValue) })
    }

    private fun describe(error: IDJIError): String =
        "${error.errorType()} ${error.errorCode()} ${error.description().orEmpty()}".trim()

    private companion object {
        val CAMERA: ComponentIndexType = ComponentIndexType.LEFT_OR_MAIN
        const val BYTES_PER_MB = 1024L * 1024L
        const val ROTATION_DURATION_S = 1.0
        /** The DJI sample's "whole list" value for `mediaFileIndex` and `count`. */
        const val ALL_FILES = -1
    }
}
