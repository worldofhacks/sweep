package org.worldofhacks.sweep.bridge.video

import android.view.SurfaceHolder
import android.view.SurfaceView
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView

/**
 * The `SurfaceView` the camera stream renders into. The Surface is handed to [camera] once
 * its size is known and taken back when the holder destroys it or the composable leaves the
 * tree, so stopping the display always releases the stream surface.
 */
@Composable
fun FpvSurface(camera: CameraStream, modifier: Modifier = Modifier) {
    AndroidView(
        modifier = modifier,
        factory = { context ->
            SurfaceView(context).apply {
                holder.addCallback(
                    object : SurfaceHolder.Callback {
                        override fun surfaceCreated(holder: SurfaceHolder) = Unit

                        override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int) {
                            camera.attachSurface(holder.surface, width, height)
                        }

                        override fun surfaceDestroyed(holder: SurfaceHolder) {
                            camera.detachSurface(holder.surface)
                        }
                    },
                )
            }
        },
        onRelease = { view -> camera.detachSurface(view.holder.surface) },
    )
}
