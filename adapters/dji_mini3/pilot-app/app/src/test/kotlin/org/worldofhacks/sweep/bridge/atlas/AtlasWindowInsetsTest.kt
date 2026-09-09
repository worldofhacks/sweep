package org.worldofhacks.sweep.bridge.atlas

import android.app.Application
import android.widget.FrameLayout
import androidx.core.graphics.Insets
import androidx.core.view.WindowInsetsCompat
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], application = Application::class)
class AtlasWindowInsetsTest {
    private fun insets(bars: Insets, cutout: Insets = Insets.NONE, keyboard: Insets = Insets.NONE) =
        WindowInsetsCompat.Builder()
            .setInsets(WindowInsetsCompat.Type.systemBars(), bars)
            .setInsets(WindowInsetsCompat.Type.displayCutout(), cutout)
            .setInsets(WindowInsetsCompat.Type.ime(), keyboard)
            .build()

    private fun padding(view: FrameLayout) =
        Insets.of(view.paddingLeft, view.paddingTop, view.paddingRight, view.paddingBottom)

    @Test fun `cutout larger than status bar remains clear and repeated delivery does not add padding`() {
        val view = FrameLayout(RuntimeEnvironment.getApplication())
        val source = insets(Insets.of(0, 66, 0, 132), Insets.of(0, 136, 0, 0))
        repeat(2) { assertSame(source, applyAtlasWindowInsets(view, source)) }
        assertEquals(Insets.of(0, 136, 0, 132), padding(view))
    }

    @Test fun `rotation replaces old top padding with the current side cutout`() {
        val view = FrameLayout(RuntimeEnvironment.getApplication())
        val portrait = insets(Insets.of(0, 66, 0, 132), Insets.of(0, 136, 0, 0))
        applyAtlasWindowInsets(view, portrait)
        applyAtlasWindowInsets(view, insets(Insets.of(0, 24, 132, 0), Insets.of(136, 0, 0, 0)))
        assertEquals(Insets.of(136, 24, 132, 0), padding(view))
        applyAtlasWindowInsets(view, portrait)
        assertEquals(Insets.of(0, 136, 0, 132), padding(view))
    }

    @Test fun `keyboard uses the larger bottom inset and closing restores the navigation bar`() {
        val view = FrameLayout(RuntimeEnvironment.getApplication())
        val bars = Insets.of(0, 66, 0, 132)
        val cutout = Insets.of(0, 136, 0, 0)
        applyAtlasWindowInsets(view, insets(bars, cutout, Insets.of(0, 0, 0, 880)))
        assertEquals(Insets.of(0, 136, 0, 880), padding(view))
        applyAtlasWindowInsets(view, insets(bars, cutout))
        assertEquals(Insets.of(0, 136, 0, 132), padding(view))
    }

    @Test
    @Config(sdk = [24, 28, 35])
    fun `ordinary screens keep their existing system bar padding`() {
        val view = FrameLayout(RuntimeEnvironment.getApplication())
        val bars = Insets.of(0, 66, 0, 132)
        applyAtlasWindowInsets(view, insets(bars))
        assertEquals(bars, padding(view))
    }
}
