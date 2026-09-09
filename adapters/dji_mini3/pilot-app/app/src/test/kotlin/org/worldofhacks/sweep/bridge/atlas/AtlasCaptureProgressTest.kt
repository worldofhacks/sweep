package org.worldofhacks.sweep.bridge.atlas

import org.junit.Assert.assertEquals
import org.junit.Test

class AtlasCaptureProgressTest {
    @Test fun `photos and videos do not skip the first scan view`() {
        val progress = AtlasCaptureProgress().saved("video").saved("photo")
        assertEquals(2, progress.total)
        assertEquals(0, progress.scanViews)
        assertEquals(1, progress.nextScanView)
    }

    @Test fun `switching modes preserves the next scan view`() {
        val progress = AtlasCaptureProgress().saved("panorama").saved("video").saved("photo")
        assertEquals(3, progress.total)
        assertEquals(1, progress.scanViews)
        assertEquals(2, progress.nextScanView)
    }

    @Test fun `each scan round advances only after eight saved scan views`() {
        var progress = AtlasCaptureProgress()
        repeat(16) { index ->
            assertEquals(index % 8 + 1, progress.nextScanView)
            progress = progress.saved("panorama").saved("photo")
        }
        assertEquals(32, progress.total)
        assertEquals(16, progress.scanViews)
        assertEquals(1, progress.nextScanView)
    }
}
