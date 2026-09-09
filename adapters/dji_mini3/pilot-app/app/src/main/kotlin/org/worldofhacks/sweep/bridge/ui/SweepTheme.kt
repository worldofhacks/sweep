package org.worldofhacks.sweep.bridge.ui

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

/** Native counterparts of console/src/tokens.css; no device-derived dynamic palette. */
internal object SweepPalette {
    val Pine = Color(0xff24634e)
    val Ink = Color(0xff182d29)
    val Mineral = Color(0xfff2f5ef)
    val Paper = Color(0xfffcfdf9)
    val Panel = Color(0xfff8faf5)
    val Muted = Color(0xff5c6c63)
    val Line = Color(0xffdce4d7)
}

private val SweepColors = lightColorScheme(
    primary = SweepPalette.Pine, onPrimary = Color.White,
    primaryContainer = Color(0xffe5eee5), onPrimaryContainer = SweepPalette.Ink,
    secondary = SweepPalette.Pine, onSecondary = Color.White,
    secondaryContainer = Color(0xffedf3ed), onSecondaryContainer = SweepPalette.Ink,
    tertiary = SweepPalette.Pine, onTertiary = Color.White,
    tertiaryContainer = Color(0xffedf3ed), onTertiaryContainer = SweepPalette.Ink,
    background = SweepPalette.Mineral, onBackground = SweepPalette.Ink,
    surface = SweepPalette.Paper, onSurface = SweepPalette.Ink,
    surfaceVariant = SweepPalette.Panel, onSurfaceVariant = SweepPalette.Muted,
    surfaceContainerLowest = SweepPalette.Paper, surfaceContainerLow = SweepPalette.Panel,
    surfaceContainer = SweepPalette.Mineral, surfaceContainerHigh = Color(0xffedf3ed),
    surfaceContainerHighest = Color(0xffe5eee5), surfaceTint = SweepPalette.Pine,
    outline = Color(0xff809084), outlineVariant = SweepPalette.Line,
    error = Color(0xffa3160b), onError = Color.White,
    errorContainer = Color(0xfffcf5f4), onErrorContainer = Color(0xffa3160b),
)

@Composable
internal fun SweepTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = SweepColors, shapes = Shapes(
        extraSmall = RoundedCornerShape(4.dp), small = RoundedCornerShape(8.dp),
        medium = RoundedCornerShape(12.dp), large = RoundedCornerShape(16.dp), extraLarge = RoundedCornerShape(20.dp),
    ), content = content)
}
