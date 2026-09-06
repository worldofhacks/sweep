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
import java.util.concurrent.atomic.AtomicLong
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import org.worldofhacks.sweep.bridge.core.flight.PortResult
import org.worldofhacks.sweep.bridge.core.frames.CameraProbe

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
class DjiCameraPort(
    private val calibratedPhotoWidthPx: Int = 0,
    private val calibratedPhotoHeightPx: Int = 0,
    private val calibratedHfovDeg: Double? = null,
    private val log: (name: String, detail: String) -> Unit,
) : CameraPort {
    private val holder = Any()
    private val lock = Any()
    private val _facts = MutableStateFlow(resetFacts())
    override val facts: StateFlow<CameraFacts> = _facts.asStateFlow()

    private var attached = false
    private var listener: ((CameraFile) -> Unit)? = null
    private val productGeneration = AtomicLong()

    @Volatile
    private var productPresent = false

    private var activeDownload: Pair<Long, DownloadListener>? = null

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

    init {
        val dimensionsPresent = calibratedPhotoWidthPx > 0 || calibratedPhotoHeightPx > 0
        require(!dimensionsPresent || (calibratedPhotoWidthPx > 0 && calibratedPhotoHeightPx > 0)) {
            "calibrated photo width and height must be positive together"
        }
        require(
            calibratedHfovDeg == null ||
                (calibratedHfovDeg.isFinite() && calibratedHfovDeg > 0.0 && calibratedHfovDeg <= 180.0),
        ) { "calibrated horizontal field of view must be in (0, 180]" }
        require(!dimensionsPresent || calibratedHfovDeg != null) {
            "photo dimensions require a measured horizontal field of view"
        }
    }

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
            // PhotoRatio reports only an aspect-ratio enum, not output pixels. Keep
            // dimensions unreported until the exact capture configuration is calibrated.
            log("Camera", "photo ratio ${ratio.name}; exact pixel dimensions unreported")
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
        val generation = productGeneration.incrementAndGet()
        val interrupted = synchronized(lock) {
            productPresent = connected
            val active = activeDownload
            activeDownload = null
            active
        }
        // Never carry facts or media-manager state across a disconnect or a product-change
        // callback. Fresh key values repopulate them for the new generation.
        _facts.value = resetFacts()
        gimbalPitch = null
        flatModeSupported = null
        storageLocation = null
        mediaEnabled = false
        interrupted?.second?.failed("product connection changed during download")
        log(
            "Camera",
            "product ${if (connected) "connected" else "disconnected"}; camera generation $generation",
        )
        if (connected) {
            refreshFacts { result ->
                if (result is PortResult.Failed) log("Camera", "initial fact refresh failed: ${result.detail}")
            }
        }
    }

    override fun refreshFacts(onResult: (PortResult) -> Unit) {
        val generation = currentGeneration(onResult) ?: return
        val keyManager = KeyManager.getInstance()
        // onResult cannot report success while the connection, active photo mode,
        // gimbal attitude, storage, or supported capability reads are merely stale.
        requiredRead(
            keyManager,
            generation,
            KeyTools.createKey(CameraKey.KeyConnection, CAMERA),
            "camera connection",
            onResult,
        ) { connected ->
            _facts.update { it.copy(cameraConnected = connected) }
            if (!connected) {
                onResult(PortResult.Failed("camera is not connected"))
            } else {
                refreshPhotoMode(keyManager, generation, onResult)
            }
        }
    }

    private fun refreshPhotoMode(
        keyManager: KeyManager,
        generation: Long,
        onResult: (PortResult) -> Unit,
    ) {
        val supportKey = KeyTools.createKey(CameraKey.KeyCameraFlatModeSupported, CAMERA)
        if (!keyManager.isKeySupported(supportKey)) {
            flatModeSupported = false
            refreshLegacyPhotoMode(keyManager, generation, onResult)
            return
        }
        requiredRead(keyManager, generation, supportKey, "flat camera mode support", onResult) { supported ->
            flatModeSupported = supported
            if (supported) {
                requiredRead(
                    keyManager,
                    generation,
                    KeyTools.createKey(CameraKey.KeyCameraFlatMode, CAMERA),
                    "flat camera mode",
                    onResult,
                ) { mode ->
                    _facts.update { it.copy(photoMode = mode == CameraFlatMode.PHOTO_NORMAL) }
                    refreshGimbal(keyManager, generation, onResult)
                }
            } else {
                refreshLegacyPhotoMode(keyManager, generation, onResult)
            }
        }
    }

    private fun refreshLegacyPhotoMode(
        keyManager: KeyManager,
        generation: Long,
        onResult: (PortResult) -> Unit,
    ) {
        requiredRead(
            keyManager,
            generation,
            KeyTools.createKey(CameraKey.KeyCameraMode, CAMERA),
            "camera mode",
            onResult,
        ) { mode ->
            _facts.update { it.copy(photoMode = mode == CameraMode.PHOTO_NORMAL) }
            refreshGimbal(keyManager, generation, onResult)
        }
    }

    private fun refreshGimbal(
        keyManager: KeyManager,
        generation: Long,
        onResult: (PortResult) -> Unit,
    ) {
        requiredRead(
            keyManager,
            generation,
            KeyTools.createKey(GimbalKey.KeyGimbalAttitude),
            "gimbal attitude",
            onResult,
        ) { attitude: Attitude ->
            gimbalPitch = attitude.pitch
            requiredRead(
                keyManager,
                generation,
                KeyTools.createKey(CameraKey.KeyCameraStorageInfos, CAMERA),
                "camera storage",
                onResult,
            ) { infos ->
                applyStorage(infos)
                finishFactRefresh(keyManager, generation, onResult)
            }
        }
    }

    private fun finishFactRefresh(
        keyManager: KeyManager,
        generation: Long,
        onResult: (PortResult) -> Unit,
    ) {
        fun capabilities() {
            optionalRead(
                keyManager,
                generation,
                KeyTools.createKey(GimbalKey.KeyGimbalAttitudeRange),
                "gimbal attitude range",
                onResult,
                apply = { range ->
                    _facts.update {
                        it.copy(
                            gimbalPitchMinDeg = range.pitch?.min,
                            gimbalPitchMaxDeg = range.pitch?.max,
                        )
                    }
                },
            ) {
                optionalRead(
                    keyManager,
                    generation,
                    KeyTools.createKey(CameraKey.KeyVisionPhotoPanoramaModeRange, CAMERA),
                    "panorama mode range",
                    onResult,
                    apply = { modes ->
                        _facts.update {
                            it.copy(
                                panoramaAdvertised = modes
                                    .filter { mode ->
                                        mode != PhotoPanoramaMode.MODE_NONE &&
                                            mode != PhotoPanoramaMode.UNKNOWN
                                    }
                                    .map { mode -> mode.name },
                            )
                        }
                    },
                ) { onResult(PortResult.Ok) }
            }
        }

        if (storageLocation != null) {
            capabilities()
            return
        }
        requiredRead(
            keyManager,
            generation,
            KeyTools.createKey(CameraKey.KeyCameraStorageLocation, CAMERA),
            "camera storage location",
            onResult,
        ) { location ->
            storageLocation = location
            capabilities()
        }
    }

    override fun gimbalPitchDeg(): Double? = gimbalPitch

    override fun setGimbalPitch(pitchDeg: Double, onResult: (PortResult) -> Unit) {
        val generation = currentGeneration(onResult) ?: return
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
        performAction(generation, KeyTools.createKey(GimbalKey.KeyRotateByAngle), rotation, onResult)
    }

    override fun enterPhotoMode(onResult: (PortResult) -> Unit) {
        val generation = currentGeneration(onResult) ?: return
        if (!mediaEnabled) {
            setPhotoMode(generation, onResult)
            return
        }
        // The media manager puts the camera in playback; release it before the shutter.
        manager.disable(
            object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    if (!isCurrent(generation)) {
                        onResult(PortResult.Failed("product connection changed while leaving media mode"))
                        return
                    }
                    mediaEnabled = false
                    setPhotoMode(generation, onResult)
                }

                override fun onFailure(error: IDJIError) {
                    if (!isCurrent(generation)) {
                        onResult(PortResult.Failed("product connection changed while leaving media mode"))
                        return
                    }
                    log("Media manager", "disable failed: ${describe(error)}; setting photo mode anyway")
                    mediaEnabled = false
                    setPhotoMode(generation, onResult)
                }
            },
        )
    }

    override fun shootPhoto(onResult: (PortResult) -> Unit) {
        val generation = currentGeneration(onResult) ?: return
        val key = KeyTools.createKey(CameraKey.KeyStartShootPhoto, CAMERA)
        val keyManager = KeyManager.getInstance()
        if (!keyManager.isKeySupported(key)) {
            onResult(PortResult.Failed("KeyStartShootPhoto is not supported by the connected product"))
            return
        }
        keyManager.performAction(
            key,
            object : CommonCallbacks.CompletionCallbackWithParam<EmptyMsg> {
                override fun onSuccess(value: EmptyMsg?) = complete(generation, onResult, PortResult.Ok)

                override fun onFailure(error: IDJIError) = complete(
                    generation,
                    onResult,
                    PortResult.Failed(describe(error)),
                )
            },
        )
    }

    override fun setFileListener(listener: ((CameraFile) -> Unit)?) {
        synchronized(lock) { this.listener = listener }
    }

    override fun download(file: CameraFile, target: File, listener: DownloadListener) {
        val generation = synchronized(lock) {
            if (!productPresent) {
                null
            } else {
                productGeneration.get().also { activeDownload = it to listener }
            }
        }
        if (generation == null) {
            listener.failed("aircraft product is not connected")
            return
        }
        val guarded = generationListener(generation, listener)
        val mediaManager = manager
        mediaManager.enable(
            object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    if (!isCurrent(generation)) return
                    mediaEnabled = true
                    pullList(generation, file, target, guarded)
                }

                override fun onFailure(error: IDJIError) = guarded.failed("media manager enable failed: ${describe(error)}")
            },
        )
    }

    override fun leaveMediaMode(onResult: (PortResult) -> Unit) {
        if (!mediaEnabled) {
            onResult(PortResult.Ok)
            return
        }
        val generation = currentGeneration(onResult) ?: return
        manager.disable(
            object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    if (!isCurrent(generation)) {
                        onResult(PortResult.Failed("product connection changed while leaving media mode"))
                        return
                    }
                    mediaEnabled = false
                    onResult(PortResult.Ok)
                }

                override fun onFailure(error: IDJIError) {
                    if (!isCurrent(generation)) {
                        onResult(PortResult.Failed("product connection changed while leaving media mode"))
                        return
                    }
                    mediaEnabled = false
                    onResult(PortResult.Failed("media manager disable failed: ${describe(error)}"))
                }
            },
        )
    }

    // ---- internals ----

    private fun setPhotoMode(generation: Long, onResult: (PortResult) -> Unit) {
        val keyManager = KeyManager.getInstance()
        val done = object : CommonCallbacks.CompletionCallback {
            override fun onSuccess() {
                if (!isCurrent(generation)) {
                    onResult(PortResult.Failed("product connection changed while setting photo mode"))
                    return
                }
                _facts.update { it.copy(photoMode = true) }
                onResult(PortResult.Ok)
            }

            override fun onFailure(error: IDJIError) = complete(
                generation,
                onResult,
                PortResult.Failed("photo mode refused: ${describe(error)}"),
            )
        }
        if (flatModeSupported == false) {
            keyManager.setValue(KeyTools.createKey(CameraKey.KeyCameraMode, CAMERA), CameraMode.PHOTO_NORMAL, done)
        } else {
            keyManager.setValue(KeyTools.createKey(CameraKey.KeyCameraFlatMode, CAMERA), CameraFlatMode.PHOTO_NORMAL, done)
        }
    }

    private fun pullList(
        generation: Long,
        file: CameraFile,
        target: File,
        listener: DownloadListener,
    ) {
        if (!isCurrent(generation)) return
        val mediaManager = manager
        val location = storageLocation
        if (location == null) {
            listener.failed("camera storage location is unreported")
            return
        }
        mediaManager.setMediaFileDataSource(MediaFileListDataSource.Builder().setLocation(location).setIndexType(CAMERA).build())
        val param = PullMediaFileListParam.Builder().mediaFileIndex(ALL_FILES).count(ALL_FILES).filter(MediaFileFilter.PHOTO).build()
        mediaManager.pullMediaFileListFromCamera(
            param,
            object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() {
                    if (!isCurrent(generation)) return
                    val files = mediaManager.mediaFileListData?.data.orEmpty()
                    val match = files.firstOrNull { it.fileIndex == file.index }
                    if (match == null) {
                        listener.failed("file index ${file.index} (${file.name}) is not in the camera's list of ${files.size} photos on $location")
                        return
                    }
                    if (match.fileSize <= 0 || match.fileSize != file.sizeBytes) {
                        listener.failed(
                            "file index ${file.index} changed size from the ${file.sizeBytes}-byte announcement " +
                                "to ${match.fileSize} bytes in the camera list",
                        )
                        return
                    }
                    log("Media manager", "downloading ${match.fileName} (${match.fileSize} bytes, index ${match.fileIndex}) to ${target.absolutePath}")
                    pullFile(generation, match, target, listener)
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
    private fun pullFile(
        generation: Long,
        media: MediaFile,
        target: File,
        listener: DownloadListener,
    ) {
        if (!isCurrent(generation)) return
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
                private val ioLock = Any()
                private var writeError: java.io.IOException? = null
                private var nextOffset = 0L
                private var closed = false

                override fun onStart() {
                    if (isCurrent(generation)) listener.progress(0, media.fileSize)
                }

                override fun onProgress(total: Long, current: Long) {
                    if (isCurrent(generation)) listener.progress(current, total)
                }

                override fun onRealtimeDataUpdate(data: ByteArray, position: Long) {
                    synchronized(ioLock) {
                        if (writeError != null || closed || !isCurrent(generation)) return
                        if (position != nextOffset) {
                            writeError = java.io.IOException(
                                "non-contiguous camera chunk: expected offset $nextOffset, received $position",
                            )
                            return
                        }
                        try {
                            stream.write(data)
                            nextOffset += data.size
                        } catch (error: java.io.IOException) {
                            writeError = error
                            log("Media manager", "write failed at $position: ${error.message}")
                        }
                    }
                }

                override fun onFinish() {
                    val (error, received) = synchronized(ioLock) {
                        if (!closed) {
                            try {
                                stream.close()
                            } catch (failure: java.io.IOException) {
                                if (writeError == null) writeError = failure
                            }
                            closed = true
                        }
                        writeError to nextOffset
                    }
                    val expected = media.fileSize
                    val written = target.length()
                    val problem = when {
                        error != null -> "download truncated: $written of $expected bytes: ${error.message}"
                        expected <= 0 -> "download unverified: the camera listed no size for ${media.fileName}; $written bytes written"
                        received != expected -> "download callbacks covered $received of $expected bytes"
                        written != expected -> "download truncated: $written of $expected bytes"
                        else -> null
                    }
                    if (!isCurrent(generation)) {
                        target.delete()
                        return
                    }
                    if (problem != null) {
                        target.delete()
                        listener.failed("$problem; partial file removed")
                        return
                    }
                    listener.finished()
                }

                override fun onFailure(error: IDJIError) {
                    synchronized(ioLock) {
                        if (!closed) runCatching { stream.close() }
                        closed = true
                    }
                    target.delete()
                    if (isCurrent(generation)) {
                        listener.failed("download failed: ${describe(error)}; partial file removed")
                    }
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

    private fun resetFacts(): CameraFacts = CameraFacts(
        horizontalFovDeg = calibratedHfovDeg ?: CameraProbe().horizontalFovDeg,
        photoWidthPx = calibratedPhotoWidthPx,
        photoHeightPx = calibratedPhotoHeightPx,
        photoDimensionsReported = calibratedPhotoWidthPx > 0 && calibratedPhotoHeightPx > 0,
    )

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

    private fun <T : Any> performAction(
        generation: Long,
        key: DJIKey.ActionKey<T, EmptyMsg>,
        parameter: T,
        onResult: (PortResult) -> Unit,
    ) {
        val keyManager = KeyManager.getInstance()
        if (!keyManager.isKeySupported(key)) {
            onResult(PortResult.Failed("${key.keyInfo.identifier} is not supported by the connected product"))
            return
        }
        keyManager.performAction(
            key,
            parameter,
            object : CommonCallbacks.CompletionCallbackWithParam<EmptyMsg> {
                override fun onSuccess(value: EmptyMsg?) = complete(generation, onResult, PortResult.Ok)

                override fun onFailure(error: IDJIError) = complete(
                    generation,
                    onResult,
                    PortResult.Failed(describe(error)),
                )
            },
        )
    }

    private fun <T : Any> optionalRead(
        keyManager: KeyManager,
        generation: Long,
        key: DJIKey<T>,
        label: String,
        onResult: (PortResult) -> Unit,
        apply: (T) -> Unit,
        done: () -> Unit,
    ) {
        if (!isCurrent(generation)) {
            onResult(PortResult.Failed("product connection changed during camera fact refresh"))
            return
        }
        if (!keyManager.isKeySupported(key)) {
            log("Camera", "$label is not supported by the connected product")
            done()
            return
        }
        keyManager.getValue(
            key,
            object : CommonCallbacks.CompletionCallbackWithParam<T> {
                override fun onSuccess(value: T?) {
                    when {
                        !isCurrent(generation) -> onResult(
                            PortResult.Failed("product connection changed during camera fact refresh"),
                        )
                        value == null -> {
                            log("Camera", "$label returned no value")
                            done()
                        }
                        else -> {
                            apply(value)
                            done()
                        }
                    }
                }

                override fun onFailure(error: IDJIError) {
                    if (isCurrent(generation)) {
                        log("Camera", "$label read failed: ${describe(error)}")
                        done()
                    } else {
                        onResult(
                            PortResult.Failed("product connection changed during camera fact refresh"),
                        )
                    }
                }
            },
        )
    }

    private fun <T : Any> requiredRead(
        keyManager: KeyManager,
        generation: Long,
        key: DJIKey<T>,
        label: String,
        onResult: (PortResult) -> Unit,
        apply: (T) -> Unit,
    ) {
        if (!isCurrent(generation)) {
            onResult(PortResult.Failed("product connection changed during camera fact refresh"))
            return
        }
        if (!keyManager.isKeySupported(key)) {
            onResult(PortResult.Failed("$label is not supported by the connected product"))
            return
        }
        keyManager.getValue(
            key,
            object : CommonCallbacks.CompletionCallbackWithParam<T> {
                override fun onSuccess(value: T?) {
                    when {
                        !isCurrent(generation) -> onResult(
                            PortResult.Failed("product connection changed during camera fact refresh"),
                        )
                        value == null -> onResult(PortResult.Failed("$label returned no value"))
                        else -> apply(value)
                    }
                }

                override fun onFailure(error: IDJIError) = onResult(
                    if (isCurrent(generation)) {
                        PortResult.Failed("$label read failed: ${describe(error)}")
                    } else {
                        PortResult.Failed("product connection changed during camera fact refresh")
                    },
                )
            },
        )
    }

    /** Listeners are registered without a support check: the product may connect after registration. */
    private fun <T : Any> listen(key: DJIKey<T>, apply: (T) -> Unit) {
        KeyManager.getInstance().listen(
            key,
            holder,
            CommonCallbacks.KeyListener<T> { _, newValue ->
                if (newValue != null && productPresent) apply(newValue)
            },
        )
    }

    private fun currentGeneration(onResult: (PortResult) -> Unit): Long? {
        val generation = synchronized(lock) {
            productGeneration.get().takeIf { productPresent }
        }
        if (generation != null) return generation
        onResult(PortResult.Failed("aircraft product is not connected"))
        return null
    }

    private fun isCurrent(generation: Long): Boolean =
        productPresent && productGeneration.get() == generation

    private fun complete(
        generation: Long,
        onResult: (PortResult) -> Unit,
        result: PortResult,
    ) {
        onResult(
            if (isCurrent(generation)) {
                result
            } else {
                PortResult.Failed("product connection changed during camera operation")
            },
        )
    }

    private fun generationListener(
        generation: Long,
        delegate: DownloadListener,
    ): DownloadListener = object : DownloadListener {
        override fun progress(bytes: Long, total: Long) {
            if (isCurrent(generation)) delegate.progress(bytes, total)
        }

        override fun finished() {
            if (finishDownload(generation, delegate)) delegate.finished()
        }

        override fun failed(detail: String) {
            if (finishDownload(generation, delegate)) delegate.failed(detail)
        }
    }

    private fun finishDownload(generation: Long, delegate: DownloadListener): Boolean =
        synchronized(lock) {
            val active = activeDownload
            if (
                !isCurrent(generation) ||
                active == null ||
                active.first != generation ||
                active.second !== delegate
            ) {
                false
            } else {
                activeDownload = null
                true
            }
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
