import numpy as np
import torch
from PIL import Image
import io
import requests
import base64
import zlib
import subprocess
import sys
import asyncio
from concurrent.futures import ThreadPoolExecutor

# Attempt to install playwright if not available
try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Playwright not found. Installing...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])
        # Also need to install browser binaries
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        from playwright.async_api import async_playwright
        print("Playwright installed successfully!")
    except Exception as e:
        print(f"Failed to install Playwright: {e}")
        print("Please install manually: pip install playwright && playwright install chromium")
        raise


class DiagramRenderNode:
    """
    A ComfyUI node that renders diagrams using Kroki API
    Supports Mermaid, PlantUML, GraphViz, and many other formats
    """
    
    # Common diagram types supported by Kroki
    DIAGRAM_TYPES = [
        "mermaid",
        "plantuml",
        "graphviz",
        "blockdiag",
        "seqdiag",
        "actdiag",
        "nwdiag",
        "packetdiag",
        "rackdiag",
        "c4plantuml",
        "d2",
        "dbml",
        "ditaa",
        "erd",
        "excalidraw",
        "nomnoml",
        "pikchr",
        "structurizr",
        "svgbob",
        "vega",
        "vegalite",
        "wavedrom",
    ]
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "diagram_code": ("STRING", {
                    "multiline": True,
                    "default": "graph TD\n    A[Start] --> B[End]"
                }),
                "diagram_type": (cls.DIAGRAM_TYPES, {
                    "default": "mermaid"
                }),
                "width": ("INT", {
                    "default": 1024,
                    "min": 100,
                    "max": 8192,
                    "step": 1
                }),
                "height": ("INT", {
                    "default": 768,
                    "min": 100,
                    "max": 8192,
                    "step": 1
                }),
                "auto_crop": ("BOOLEAN", {
                    "default": True
                }),
                "background_color": (["transparent", "white", "black", "custom"],),
            },
            "optional": {
                "custom_bg_color": ("STRING", {
                    "default": "#ffffff",
                    "multiline": False
                }),
                "kroki_url": ("STRING", {
                    "default": "https://kroki.io",
                    "multiline": False
                }),
            }
        }
    
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "render_diagram"
    CATEGORY = "image/generation"

    def render_via_kroki(self, diagram_code, diagram_type, kroki_url):
        """Render using Kroki API - request SVG for quality"""
        # Kroki expects base64-encoded, deflated content
        compressed = zlib.compress(diagram_code.encode('utf-8'), 9)
        encoded = base64.urlsafe_b64encode(compressed).decode('utf-8')
        
        # Request SVG - all diagram types support it
        url = f"{kroki_url.rstrip('/')}/{diagram_type}/svg/{encoded}"
        
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as e:
            raise Exception(f"Failed to render diagram via Kroki: {str(e)}")

    def _render_svg_sync(self, svg_text, width, height, bg_color):
        """Synchronous wrapper that runs Playwright in a new thread with its own event loop"""
        async def _render():
            # Process SVG to remove fixed dimensions and ensure scaling
            import re
            
            # Extract viewBox if it exists (we want to preserve this)
            viewbox_match = re.search(r'viewBox\s*=\s*["\']([^"\']*)["\']', svg_text)
            
            # Remove width, height, and style attributes from svg tag
            svg_text_processed = re.sub(r'<svg([^>]*)\s+width\s*=\s*["\'][^"\']*["\']', r'<svg\1', svg_text)
            svg_text_processed = re.sub(r'<svg([^>]*)\s+height\s*=\s*["\'][^"\']*["\']', r'<svg\1', svg_text_processed)
            svg_text_processed = re.sub(r'<svg([^>]*)\s+style\s*=\s*["\'][^"\']*["\']', r'<svg\1', svg_text_processed)
            
            # If there's no viewBox, try to add one based on width/height that were removed
            if not viewbox_match:
                # Try to extract original dimensions
                width_match = re.search(r'width\s*=\s*["\']?(\d+(?:\.\d+)?)', svg_text)
                height_match = re.search(r'height\s*=\s*["\']?(\d+(?:\.\d+)?)', svg_text)
                
                if width_match and height_match:
                    orig_width = width_match.group(1)
                    orig_height = height_match.group(1)
                    # Add viewBox to the processed SVG
                    svg_text_processed = re.sub(
                        r'<svg',
                        f'<svg viewBox="0 0 {orig_width} {orig_height}"',
                        svg_text_processed,
                        count=1
                    )
            
            # Create HTML wrapper for SVG with proper sizing and background
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <style>
                    html, body {{
                        margin: 0;
                        padding: 0;
                        width: 100%;
                        height: 100%;
                        overflow: hidden;
                        background-color: {bg_color if bg_color != "transparent" else "white"};
                    }}
                    body {{
                        display: flex;
                        align-items: center;
                        justify-content: center;
                    }}
                    svg {{
                        width: 100% !important;
                        height: 100% !important;
                        max-width: 100%;
                        max-height: 100%;
                        object-fit: contain;
                    }}
                </style>
            </head>
            <body>
                {svg_text_processed}
            </body>
            </html>
            """
            
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={'width': width, 'height': height})
                
                # Set content and wait for rendering
                await page.set_content(html_content)
                await page.wait_for_load_state('networkidle')
                
                # Take screenshot
                png_bytes = await page.screenshot(
                    full_page=False,
                    omit_background=(bg_color == "transparent")
                )
                
                await browser.close()
                
                return png_bytes
        
        # Run in a new thread with its own event loop
        def run_in_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_render())
            finally:
                loop.close()
        
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_in_thread)
            return future.result()

    def render_diagram(self, diagram_code, diagram_type, width, height, auto_crop, background_color, 
                      custom_bg_color="#ffffff", kroki_url="https://kroki.io"):
        
        # Render diagram via Kroki (get SVG)
        svg_bytes = self.render_via_kroki(diagram_code, diagram_type, kroki_url)
        svg_text = svg_bytes.decode('utf-8')
        
        # Determine background color
        if background_color == "transparent":
            bg_color = "transparent"
        elif background_color == "white":
            bg_color = "#ffffff"
        elif background_color == "black":
            bg_color = "#000000"
        elif background_color == "custom":
            bg_color = custom_bg_color
        else:
            bg_color = "#ffffff"
        
        # Render SVG using Playwright in a separate thread
        try:
            png_bytes = self._render_svg_sync(svg_text, width, height, bg_color)
                
        except Exception as e:
            raise Exception(
                f"Failed to render SVG using Playwright: {str(e)}. "
                "Please ensure Playwright is installed: pip install playwright && playwright install chromium"
            )
        
        # Convert bytes to PIL Image
        image = Image.open(io.BytesIO(png_bytes))
        
        # Auto-crop to remove excess background
        if auto_crop:
            # Convert to numpy for processing
            img_array = np.array(image)
            
            # Find bounding box of non-background pixels
            # For white background, find non-white pixels
            if bg_color == "#ffffff" or bg_color == "white":
                # Find pixels that are not white (allowing small tolerance for anti-aliasing)
                mask = np.any(img_array < 250, axis=-1) if len(img_array.shape) == 3 else img_array < 250
            elif bg_color == "#000000" or bg_color == "black":
                # Find pixels that are not black
                mask = np.any(img_array > 5, axis=-1) if len(img_array.shape) == 3 else img_array > 5
            else:
                # For other colors or transparent, look for any non-background pixels
                if image.mode == 'RGBA':
                    # Use alpha channel
                    mask = img_array[:, :, 3] > 10
                else:
                    # Generic approach: find pixels different from corners
                    corner_color = img_array[0, 0]
                    mask = np.any(np.abs(img_array - corner_color) > 10, axis=-1) if len(img_array.shape) == 3 else np.abs(img_array - corner_color) > 10
            
            # Find bounding box
            rows = np.any(mask, axis=1)
            cols = np.any(mask, axis=0)
            
            if rows.any() and cols.any():
                ymin, ymax = np.where(rows)[0][[0, -1]]
                xmin, xmax = np.where(cols)[0][[0, -1]]
                
                # Add small padding (2% of image size)
                padding_x = max(5, int((xmax - xmin) * 0.02))
                padding_y = max(5, int((ymax - ymin) * 0.02))
                
                xmin = max(0, xmin - padding_x)
                ymin = max(0, ymin - padding_y)
                xmax = min(image.width - 1, xmax + padding_x)
                ymax = min(image.height - 1, ymax + padding_y)
                
                # Crop the image
                image = image.crop((xmin, ymin, xmax + 1, ymax + 1))
        
        # Ensure RGB mode for ComfyUI
        if image.mode != 'RGB':
            if image.mode == 'RGBA':
                # Determine background for compositing
                if bg_color == "transparent" or bg_color == "#ffffff":
                    comp_bg = (255, 255, 255)
                elif bg_color == "#000000":
                    comp_bg = (0, 0, 0)
                else:
                    # Parse hex color
                    comp_bg = tuple(int(bg_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
                
                # Composite on background
                background = Image.new('RGB', image.size, comp_bg)
                background.paste(image, mask=image.split()[3])
                image = background
            else:
                image = image.convert('RGB')
        
        # Convert PIL Image to numpy array
        image_np = np.array(image).astype(np.float32) / 255.0
        
        # Convert to torch tensor with shape (batch, height, width, channels)
        image_tensor = torch.from_numpy(image_np)[None,]
        
        return (image_tensor,)


# Node class mappings for ComfyUI
NODE_CLASS_MAPPINGS = {
    "DiagramRenderNode": DiagramRenderNode
}

# Human-readable names
NODE_DISPLAY_NAME_MAPPINGS = {
    "DiagramRenderNode": "Render Diagram (Kroki)"
}
