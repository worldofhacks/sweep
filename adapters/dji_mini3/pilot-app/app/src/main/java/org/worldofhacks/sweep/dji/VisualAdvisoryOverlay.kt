package org.worldofhacks.sweep.dji

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.view.View

class VisualAdvisoryOverlay(context: Context) : View(context) {
    private var advisory = FeedAdvisory(
        coverage = FeedCoverage.NONE,
        quality = FeedQuality.UNKNOWN,
        readiness = FeedReadiness.NO_SURFACE,
    )
    private val reticlePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        strokeWidth = 3f
        style = Paint.Style.STROKE
    }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        textSize = 34f
        style = Paint.Style.FILL
    }
    private val panelPaint = Paint().apply {
        color = 0x99000000.toInt()
        style = Paint.Style.FILL
    }

    fun show(advisory: FeedAdvisory) {
        this.advisory = advisory
        invalidate()
    }

    fun currentAdvisory(): FeedAdvisory = advisory

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val centerX = width / 2f
        val centerY = height / 2f
        canvas.drawCircle(centerX, centerY, 24f, reticlePaint)
        canvas.drawLine(centerX - 44f, centerY, centerX + 44f, centerY, reticlePaint)
        canvas.drawLine(centerX, centerY - 44f, centerX, centerY + 44f, reticlePaint)

        canvas.drawRect(24f, 24f, 580f, 166f, panelPaint)
        canvas.drawText("Coverage: ${advisory.coverage.name.lowercase()}", 42f, 66f, textPaint)
        canvas.drawText("Quality: ${qualityLabel(advisory.quality)}", 42f, 108f, textPaint)
        canvas.drawText("Readiness: ${advisory.readiness.name.lowercase()}", 42f, 150f, textPaint)
    }

    private fun qualityLabel(quality: FeedQuality): String =
        if (quality == FeedQuality.UNKNOWN) {
            "unknown"
        } else {
            "${quality.width}x${quality.height} @ ${quality.framesPerSecond} fps"
        }
}
