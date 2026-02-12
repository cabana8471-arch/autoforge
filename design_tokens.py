"""
Design Tokens Management
========================

Manages design tokens for consistent styling across projects.

Features:
- Parse design tokens from app_spec.txt or JSON config
- Generate CSS custom properties
- Generate Tailwind CSS configuration
- Generate SCSS variables
- Validate color contrast ratios
- Support for light/dark themes
- Style presets for visual styles (neobrutalism, glassmorphism, retro)

Configuration:
- design_tokens section in app_spec.txt
- .autocoder/design-tokens.json for custom tokens
- visual_style section for preset-based tokens

Token Categories:
- Colors (primary, secondary, accent, neutral, semantic)
- Spacing (scale, gutters, margins)
- Typography (fonts, sizes, weights, line-heights)
- Borders (radii, widths)
- Shadows
- Animations (durations, easings)
"""

import colorsys
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ColorToken:
    """A color token with variants."""

    name: str
    value: str  # Hex color
    variants: dict = field(default_factory=dict)  # 50-950 shades

    def to_hsl(self) -> tuple[float, float, float]:
        """Convert hex to HSL."""
        hex_color = self.value.lstrip("#")
        r, g, b = tuple(int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
        hue, lightness, sat = colorsys.rgb_to_hls(r, g, b)
        return (hue * 360, sat * 100, lightness * 100)

    def generate_shades(self) -> dict:
        """Generate 50-950 shades from base color."""
        hue, sat, lightness = self.to_hsl()

        shades = {
            "50": self._hsl_to_hex(hue, max(10, sat * 0.3), 95),
            "100": self._hsl_to_hex(hue, max(15, sat * 0.5), 90),
            "200": self._hsl_to_hex(hue, max(20, sat * 0.6), 80),
            "300": self._hsl_to_hex(hue, max(25, sat * 0.7), 70),
            "400": self._hsl_to_hex(hue, max(30, sat * 0.85), 60),
            "500": self.value,  # Base color
            "600": self._hsl_to_hex(hue, min(100, sat * 1.1), lightness * 0.85),
            "700": self._hsl_to_hex(hue, min(100, sat * 1.15), lightness * 0.7),
            "800": self._hsl_to_hex(hue, min(100, sat * 1.2), lightness * 0.55),
            "900": self._hsl_to_hex(hue, min(100, sat * 1.25), lightness * 0.4),
            "950": self._hsl_to_hex(hue, min(100, sat * 1.3), lightness * 0.25),
        }
        return shades

    def _hsl_to_hex(self, hue: float, sat: float, lightness: float) -> str:
        """Convert HSL to hex."""
        r, g, b = colorsys.hls_to_rgb(hue / 360, lightness / 100, sat / 100)
        return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


@dataclass
class DesignTokens:
    """Complete design token system."""

    colors: dict = field(default_factory=dict)
    spacing: list = field(default_factory=lambda: [4, 8, 12, 16, 24, 32, 48, 64, 96])
    typography: dict = field(default_factory=dict)
    borders: dict = field(default_factory=dict)
    shadows: dict = field(default_factory=dict)
    animations: dict = field(default_factory=dict)

    @classmethod
    def default(cls) -> "DesignTokens":
        """Create default design tokens."""
        return cls(
            colors={
                "primary": "#3B82F6",  # Blue
                "secondary": "#6366F1",  # Indigo
                "accent": "#F59E0B",  # Amber
                "success": "#10B981",  # Emerald
                "warning": "#F59E0B",  # Amber
                "error": "#EF4444",  # Red
                "info": "#3B82F6",  # Blue
                "neutral": "#6B7280",  # Gray
            },
            spacing=[4, 8, 12, 16, 24, 32, 48, 64, 96],
            typography={
                "font_family": {
                    "sans": "Inter, system-ui, sans-serif",
                    "mono": "JetBrains Mono, monospace",
                },
                "font_size": {
                    "xs": "0.75rem",
                    "sm": "0.875rem",
                    "base": "1rem",
                    "lg": "1.125rem",
                    "xl": "1.25rem",
                    "2xl": "1.5rem",
                    "3xl": "1.875rem",
                    "4xl": "2.25rem",
                },
                "font_weight": {
                    "normal": "400",
                    "medium": "500",
                    "semibold": "600",
                    "bold": "700",
                },
                "line_height": {
                    "tight": "1.25",
                    "normal": "1.5",
                    "relaxed": "1.75",
                },
            },
            borders={
                "radius": {
                    "none": "0",
                    "sm": "0.125rem",
                    "md": "0.375rem",
                    "lg": "0.5rem",
                    "xl": "0.75rem",
                    "2xl": "1rem",
                    "full": "9999px",
                },
                "width": {
                    "0": "0",
                    "1": "1px",
                    "2": "2px",
                    "4": "4px",
                },
            },
            shadows={
                "sm": "0 1px 2px 0 rgb(0 0 0 / 0.05)",
                "md": "0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1)",
                "lg": "0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1)",
                "xl": "0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1)",
            },
            animations={
                "duration": {
                    "fast": "150ms",
                    "normal": "300ms",
                    "slow": "500ms",
                },
                "easing": {
                    "linear": "linear",
                    "ease-in": "cubic-bezier(0.4, 0, 1, 1)",
                    "ease-out": "cubic-bezier(0, 0, 0.2, 1)",
                    "ease-in-out": "cubic-bezier(0.4, 0, 0.2, 1)",
                },
            },
        )


class DesignTokensManager:
    """
    Manages design tokens for a project.

    Usage:
        manager = DesignTokensManager(project_dir)
        tokens = manager.load()
        manager.generate_css(tokens)
        manager.generate_tailwind_config(tokens)
    """

    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.config_path = self.project_dir / ".autocoder" / "design-tokens.json"

    def load(self) -> DesignTokens:
        """
        Load design tokens from config file or app_spec.txt.

        Returns:
            DesignTokens instance
        """
        # Try to load from config file
        if self.config_path.exists():
            return self._load_from_config()

        # Try to parse from app_spec.txt
        app_spec = self.project_dir / "prompts" / "app_spec.txt"
        if app_spec.exists():
            tokens = self._parse_from_app_spec(app_spec)
            if tokens:
                return tokens

        # Return defaults
        return DesignTokens.default()

    def _load_from_config(self) -> DesignTokens:
        """Load tokens from JSON config."""
        try:
            data = json.loads(self.config_path.read_text())
            return DesignTokens(
                colors=data.get("colors", {}),
                spacing=data.get("spacing", [4, 8, 12, 16, 24, 32, 48, 64, 96]),
                typography=data.get("typography", {}),
                borders=data.get("borders", {}),
                shadows=data.get("shadows", {}),
                animations=data.get("animations", {}),
            )
        except Exception as e:
            logger.warning(f"Error loading design tokens config: {e}")
            return DesignTokens.default()

    def _parse_from_app_spec(self, app_spec_path: Path) -> Optional[DesignTokens]:
        """Parse design tokens from app_spec.txt."""
        try:
            content = app_spec_path.read_text()

            # Find design_tokens section
            match = re.search(r"<design_tokens[^>]*>(.*?)</design_tokens>", content, re.DOTALL)
            if not match:
                return None

            tokens_content = match.group(1)
            tokens = DesignTokens.default()

            # Parse colors
            colors_match = re.search(r"<colors[^>]*>(.*?)</colors>", tokens_content, re.DOTALL)
            if colors_match:
                for color_match in re.finditer(r"<(\w+)>([^<]+)</\1>", colors_match.group(1)):
                    tokens.colors[color_match.group(1)] = color_match.group(2).strip()

            # Parse spacing
            spacing_match = re.search(r"<spacing[^>]*>(.*?)</spacing>", tokens_content, re.DOTALL)
            if spacing_match:
                scale_match = re.search(r"<scale>\s*\[([^\]]+)\]", spacing_match.group(1))
                if scale_match:
                    tokens.spacing = [int(x.strip()) for x in scale_match.group(1).split(",")]

            # Parse typography
            typo_match = re.search(r"<typography[^>]*>(.*?)</typography>", tokens_content, re.DOTALL)
            if typo_match:
                font_match = re.search(r"<font_family>([^<]+)</font_family>", typo_match.group(1))
                if font_match:
                    tokens.typography["font_family"] = {"sans": font_match.group(1).strip()}

            return tokens

        except Exception as e:
            logger.warning(f"Error parsing app_spec.txt for design tokens: {e}")
            return None

    def save(self, tokens: DesignTokens) -> Path:
        """
        Save design tokens to config file.

        Args:
            tokens: DesignTokens to save

        Returns:
            Path to saved file
        """
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "colors": tokens.colors,
            "spacing": tokens.spacing,
            "typography": tokens.typography,
            "borders": tokens.borders,
            "shadows": tokens.shadows,
            "animations": tokens.animations,
        }

        self.config_path.write_text(json.dumps(data, indent=2))
        return self.config_path

    def generate_css(self, tokens: DesignTokens, output_path: Optional[Path] = None) -> str:
        """
        Generate CSS custom properties from design tokens.

        Args:
            tokens: DesignTokens to convert
            output_path: Optional path to write CSS file

        Returns:
            CSS content
        """
        lines = [
            "/* Design Tokens - Auto-generated by Autocoder */",
            "/* Do not edit directly - modify .autocoder/design-tokens.json instead */",
            "",
            ":root {",
        ]

        # Colors with shades
        lines.append("  /* Colors */")
        for name, value in tokens.colors.items():
            color_token = ColorToken(name=name, value=value)
            shades = color_token.generate_shades()

            lines.append(f"  --color-{name}: {value};")
            for shade, shade_value in shades.items():
                lines.append(f"  --color-{name}-{shade}: {shade_value};")

        # Spacing
        lines.append("")
        lines.append("  /* Spacing */")
        for i, space in enumerate(tokens.spacing):
            lines.append(f"  --spacing-{i}: {space}px;")

        # Typography
        lines.append("")
        lines.append("  /* Typography */")
        if "font_family" in tokens.typography:
            for name, value in tokens.typography["font_family"].items():
                lines.append(f"  --font-{name}: {value};")

        if "font_size" in tokens.typography:
            for name, value in tokens.typography["font_size"].items():
                lines.append(f"  --text-{name}: {value};")

        if "font_weight" in tokens.typography:
            for name, value in tokens.typography["font_weight"].items():
                lines.append(f"  --font-weight-{name}: {value};")

        if "line_height" in tokens.typography:
            for name, value in tokens.typography["line_height"].items():
                lines.append(f"  --leading-{name}: {value};")

        # Borders
        lines.append("")
        lines.append("  /* Borders */")
        if "radius" in tokens.borders:
            for name, value in tokens.borders["radius"].items():
                lines.append(f"  --radius-{name}: {value};")

        if "width" in tokens.borders:
            for name, value in tokens.borders["width"].items():
                lines.append(f"  --border-{name}: {value};")

        # Shadows
        lines.append("")
        lines.append("  /* Shadows */")
        for name, value in tokens.shadows.items():
            lines.append(f"  --shadow-{name}: {value};")

        # Animations
        lines.append("")
        lines.append("  /* Animations */")
        if "duration" in tokens.animations:
            for name, value in tokens.animations["duration"].items():
                lines.append(f"  --duration-{name}: {value};")

        if "easing" in tokens.animations:
            for name, value in tokens.animations["easing"].items():
                lines.append(f"  --ease-{name}: {value};")

        lines.append("}")

        css_content = "\n".join(lines)

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(css_content)

        return css_content

    def generate_tailwind_config(self, tokens: DesignTokens, output_path: Optional[Path] = None) -> str:
        """
        Generate Tailwind CSS configuration from design tokens.

        Args:
            tokens: DesignTokens to convert
            output_path: Optional path to write config file

        Returns:
            JavaScript config content
        """
        # Build color config with shades
        colors = {}
        for name, value in tokens.colors.items():
            color_token = ColorToken(name=name, value=value)
            shades = color_token.generate_shades()
            colors[name] = {
                "DEFAULT": value,
                **shades,
            }

        # Build spacing config
        spacing = {}
        for i, space in enumerate(tokens.spacing):
            spacing[str(i)] = f"{space}px"
            spacing[str(space)] = f"{space}px"

        # Build the config
        config = {
            "theme": {
                "extend": {
                    "colors": colors,
                    "spacing": spacing,
                    "fontFamily": tokens.typography.get("font_family", {}),
                    "fontSize": tokens.typography.get("font_size", {}),
                    "fontWeight": tokens.typography.get("font_weight", {}),
                    "lineHeight": tokens.typography.get("line_height", {}),
                    "borderRadius": tokens.borders.get("radius", {}),
                    "borderWidth": tokens.borders.get("width", {}),
                    "boxShadow": tokens.shadows,
                    "transitionDuration": tokens.animations.get("duration", {}),
                    "transitionTimingFunction": tokens.animations.get("easing", {}),
                }
            }
        }

        # Format as JavaScript
        config_json = json.dumps(config, indent=2)
        js_content = f"""/** @type {{import('tailwindcss').Config}} */
// Design Tokens - Auto-generated by Autocoder
// Modify .autocoder/design-tokens.json to update

module.exports = {config_json}
"""

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(js_content)

        return js_content

    def generate_scss(self, tokens: DesignTokens, output_path: Optional[Path] = None) -> str:
        """
        Generate SCSS variables from design tokens.

        Args:
            tokens: DesignTokens to convert
            output_path: Optional path to write SCSS file

        Returns:
            SCSS content
        """
        lines = [
            "// Design Tokens - Auto-generated by Autocoder",
            "// Do not edit directly - modify .autocoder/design-tokens.json instead",
            "",
            "// Colors",
        ]

        for name, value in tokens.colors.items():
            color_token = ColorToken(name=name, value=value)
            shades = color_token.generate_shades()

            lines.append(f"$color-{name}: {value};")
            for shade, shade_value in shades.items():
                lines.append(f"$color-{name}-{shade}: {shade_value};")

        lines.append("")
        lines.append("// Spacing")
        for i, space in enumerate(tokens.spacing):
            lines.append(f"$spacing-{i}: {space}px;")

        lines.append("")
        lines.append("// Typography")
        if "font_family" in tokens.typography:
            for name, value in tokens.typography["font_family"].items():
                lines.append(f"$font-{name}: {value};")

        if "font_size" in tokens.typography:
            for name, value in tokens.typography["font_size"].items():
                lines.append(f"$text-{name}: {value};")

        lines.append("")
        lines.append("// Borders")
        if "radius" in tokens.borders:
            for name, value in tokens.borders["radius"].items():
                lines.append(f"$radius-{name}: {value};")

        lines.append("")
        lines.append("// Shadows")
        for name, value in tokens.shadows.items():
            lines.append(f"$shadow-{name}: {value};")

        scss_content = "\n".join(lines)

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(scss_content)

        return scss_content

    def validate_contrast(self, tokens: DesignTokens) -> list[dict]:
        """
        Validate color contrast ratios for accessibility.

        Args:
            tokens: DesignTokens to validate

        Returns:
            List of contrast issues
        """
        issues = []

        # Check primary colors against white/black backgrounds
        for name, value in tokens.colors.items():
            color_token = ColorToken(name=name, value=value)
            _hue, _sat, lightness = color_token.to_hsl()

            # Simple contrast check based on lightness
            if lightness > 50:
                # Light color - should contrast with white
                if lightness > 85:
                    issues.append(
                        {
                            "color": name,
                            "value": value,
                            "issue": "Color may not have sufficient contrast with white background",
                            "suggestion": "Use darker shade for text on white",
                        }
                    )
            else:
                # Dark color - should contrast with black
                if lightness < 15:
                    issues.append(
                        {
                            "color": name,
                            "value": value,
                            "issue": "Color may not have sufficient contrast with dark background",
                            "suggestion": "Use lighter shade for text on dark",
                        }
                    )

        return issues

    def generate_all(self, output_dir: Optional[Path] = None) -> dict:
        """
        Generate all token files.

        Args:
            output_dir: Output directory (default: project root styles/)

        Returns:
            Dict with paths to generated files
        """
        tokens = self.load()
        output = output_dir or self.project_dir / "src" / "styles"

        # Generate files and store paths (not content)
        css_path = output / "tokens.css"
        scss_path = output / "_tokens.scss"

        self.generate_css(tokens, css_path)
        self.generate_scss(tokens, scss_path)

        # Results dict can contain string paths or list of contrast issues
        results: dict[str, str | list[str]] = {
            "css": str(css_path),
            "scss": str(scss_path),
        }

        # Check for Tailwind
        if (self.project_dir / "tailwind.config.js").exists() or (
            self.project_dir / "tailwind.config.ts"
        ).exists():
            tailwind_path = output / "tailwind.tokens.js"
            self.generate_tailwind_config(tokens, tailwind_path)
            results["tailwind"] = str(tailwind_path)

        # Validate and report
        issues = self.validate_contrast(tokens)
        if issues:
            results["contrast_issues"] = [str(i) for i in issues]

        return results


def generate_all_tokens(project_dir: Path) -> dict:
    """
    Generate all design token files for a project using DesignTokensManager.

    Args:
        project_dir: Project directory

    Returns:
        Dict with paths to generated files
    """
    manager = DesignTokensManager(project_dir)
    return manager.generate_all()


# =============================================================================
# Style Presets for Visual Styles
# =============================================================================
# These presets are used by generate_design_tokens() to create style-specific
# design tokens based on the visual_style setting in app_spec.txt

from app_spec_parser import VALID_VISUAL_STYLES, get_ui_config_from_spec

STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "neobrutalism": {
        "description": "Bold colors, hard shadows, no border-radius",
        "borders": {
            "width": "4px",
            "radius": "0",
            "style": "solid",
            "color": "currentColor",
        },
        "shadows": {
            "default": "4px 4px 0 0 currentColor",
            "hover": "6px 6px 0 0 currentColor",
            "active": "2px 2px 0 0 currentColor",
        },
        "colors": {
            "primary": "#ff6b6b",
            "secondary": "#4ecdc4",
            "accent": "#ffe66d",
            "background": "#ffffff",
            "surface": "#f8f9fa",
            "text": "#000000",
            "border": "#000000",
        },
        "typography": {
            "fontFamily": "'Inter', 'Helvetica Neue', sans-serif",
            "fontWeight": {
                "normal": "500",
                "bold": "800",
            },
        },
        "spacing": {
            "base": "8px",
            "scale": 1.5,
        },
        "effects": {
            "transition": "all 0.1s ease-in-out",
        },
    },
    "glassmorphism": {
        "description": "Frosted glass effects, blur, transparency",
        "borders": {
            "width": "1px",
            "radius": "16px",
            "style": "solid",
            "color": "rgba(255, 255, 255, 0.2)",
        },
        "shadows": {
            "default": "0 8px 32px 0 rgba(31, 38, 135, 0.15)",
            "hover": "0 12px 40px 0 rgba(31, 38, 135, 0.2)",
            "active": "0 4px 16px 0 rgba(31, 38, 135, 0.1)",
        },
        "colors": {
            "primary": "#8b5cf6",
            "secondary": "#06b6d4",
            "accent": "#f472b6",
            "background": "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
            "surface": "rgba(255, 255, 255, 0.1)",
            "text": "#ffffff",
            "border": "rgba(255, 255, 255, 0.2)",
        },
        "typography": {
            "fontFamily": "'Inter', system-ui, sans-serif",
            "fontWeight": {
                "normal": "400",
                "bold": "600",
            },
        },
        "spacing": {
            "base": "8px",
            "scale": 1.5,
        },
        "effects": {
            "backdropBlur": "12px",
            "backdropSaturate": "180%",
            "transition": "all 0.3s ease",
        },
    },
    "retro": {
        "description": "Pixel-art inspired, vibrant neons, 8-bit aesthetic",
        "borders": {
            "width": "3px",
            "radius": "0",
            "style": "solid",
            "color": "#00ffff",
        },
        "shadows": {
            "default": "0 0 10px #ff00ff, 0 0 20px #00ffff",
            "hover": "0 0 15px #ff00ff, 0 0 30px #00ffff",
            "active": "0 0 5px #ff00ff, 0 0 10px #00ffff",
        },
        "colors": {
            "primary": "#ff00ff",
            "secondary": "#00ffff",
            "accent": "#ffff00",
            "background": "#0a0a0a",
            "surface": "#1a1a2e",
            "text": "#00ff00",
            "border": "#00ffff",
        },
        "typography": {
            "fontFamily": "'Press Start 2P', 'Courier New', monospace",
            "fontWeight": {
                "normal": "400",
                "bold": "400",
            },
            "letterSpacing": "0.05em",
            "textTransform": "uppercase",
        },
        "spacing": {
            "base": "8px",
            "scale": 2,
        },
        "effects": {
            "textShadow": "0 0 5px currentColor",
            "transition": "all 0.15s steps(3)",
        },
    },
}


def get_style_preset(style: str) -> dict[str, Any] | None:
    """
    Get design tokens for a specific visual style.

    Args:
        style: The style name (neobrutalism, glassmorphism, retro)

    Returns:
        Design tokens dict or None if style is not found or is 'default'.
    """
    if style == "default" or style not in STYLE_PRESETS:
        return None
    return STYLE_PRESETS[style]


def generate_design_tokens(project_dir: Path, style: str) -> Path | None:
    """
    Generate design tokens JSON file for a project based on visual style.

    Args:
        project_dir: Path to the project directory
        style: The visual style to use

    Returns:
        Path to the generated tokens file, or None if style is default/custom
        or if file write fails.
    """
    # "default" uses library defaults, no tokens needed
    # "custom" means user will define their own tokens manually
    if style == "default" or style == "custom":
        return None

    preset = get_style_preset(style)
    if not preset:
        return None

    # Create .autocoder directory if it doesn't exist
    autocoder_dir = project_dir / ".autocoder"
    autocoder_dir.mkdir(parents=True, exist_ok=True)

    # Write design tokens
    tokens_path = autocoder_dir / "design-tokens.json"
    try:
        tokens_path.write_text(json.dumps(preset, indent=2), encoding="utf-8")
    except OSError:
        # File write failed (permissions, disk full, etc.)
        return None

    return tokens_path


def generate_design_tokens_from_spec(project_dir: Path) -> Path | None:
    """
    Generate design tokens based on project's app_spec.txt.

    Args:
        project_dir: Path to the project directory

    Returns:
        Path to the generated tokens file, or None if no tokens needed.
    """
    ui_config = get_ui_config_from_spec(project_dir)
    if not ui_config:
        return None

    style = ui_config.get("style", "default")
    return generate_design_tokens(project_dir, style)


def validate_visual_style(style: str) -> bool:
    """
    Check if a visual style is valid.

    Args:
        style: The style name to validate

    Returns:
        True if valid, False otherwise.
    """
    return style in VALID_VISUAL_STYLES
