package org.worldofhacks.sweep.dji

import android.view.Surface
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.video.channel.VideoChannelType
import dji.v5.common.video.interfaces.IVideoChannel
import dji.v5.manager.datacenter.MediaDataCenter
import dji.v5.manager.datacenter.media.MediaFileListData
import dji.v5.manager.datacenter.media.PullMediaFileListParam
import dji.v5.manager.interfaces.ICameraStreamManager

class DjiMediaAndVideo {
    private val streamManager = MediaDataCenter.getInstance().cameraStreamManager
    private var availabilityListener: ICameraStreamManager.AvailableCameraUpdatedListener? = null

    fun cameraChannel(): IVideoChannel? = MediaDataCenter.getInstance()
        .videoStreamManager
        .getAvailableVideoChannel(VideoChannelType.PRIMARY_STREAM_CHANNEL)

    fun attachPrimarySurface(surface: Surface, width: Int, height: Int) {
        require(width > 0 && height > 0)
        streamManager.putCameraStreamSurface(
            ComponentIndexType.LEFT_OR_MAIN,
            surface,
            width,
            height,
            ICameraStreamManager.ScaleType.CENTER_INSIDE,
        )
    }

    fun detachSurface(surface: Surface) {
        streamManager.removeCameraStreamSurface(surface)
    }

    fun primaryFrameQuality(): FeedQuality {
        val info = streamManager.getAircraftStreamFrameInfo(ComponentIndexType.LEFT_OR_MAIN)
            ?: return FeedQuality.UNKNOWN
        if (info.width <= 0 || info.height <= 0) return FeedQuality.UNKNOWN
        return FeedQuality(info.width, info.height, info.frameRate.coerceAtLeast(0))
    }

    fun startCameraAvailability(onChanged: (Boolean) -> Unit) {
        stopCameraAvailability()
        val listener = object : ICameraStreamManager.AvailableCameraUpdatedListener {
            override fun onAvailableCameraUpdated(cameras: MutableList<ComponentIndexType>) {
                onChanged(ComponentIndexType.LEFT_OR_MAIN in cameras)
            }

            override fun onCameraStreamEnableUpdate(
                cameraStreamEnableMap: MutableMap<ComponentIndexType, Boolean>,
            ) {
                onChanged(cameraStreamEnableMap[ComponentIndexType.LEFT_OR_MAIN] == true)
            }
        }
        availabilityListener = listener
        streamManager.addAvailableCameraUpdatedListener(listener)
    }

    fun stopCameraAvailability() {
        availabilityListener?.let(streamManager::removeAvailableCameraUpdatedListener)
        availabilityListener = null
    }

    fun mediaList(): MediaFileListData = MediaDataCenter.getInstance().mediaManager.mediaFileListData

    fun refreshMedia(callback: CommonCallbacks.CompletionCallback) {
        MediaDataCenter.getInstance().mediaManager.pullMediaFileListFromCamera(
            PullMediaFileListParam.Builder().mediaFileIndex(0).count(50).build(),
            callback,
        )
    }
}
