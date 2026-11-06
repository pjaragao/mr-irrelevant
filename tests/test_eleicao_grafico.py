"""Offline test for the election chart (league/eleicao_grafico.py). Needs Pillow (present in the bot's container)."""
import os
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "league"))

try:
    import PIL  # noqa: F401
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


@unittest.skipUnless(HAS_PIL, "Pillow não instalado (roda no container do bot)")
class GraficoTest(unittest.TestCase):
    def test_renders_all_states(self):
        import eleicao_grafico as G
        from PIL import Image
        hist = [[i, x, 49 + 0.1 * i, 0.5 / (i + 1), 42 + i] for i, x in enumerate((0.5, 1, 2, 5, 10))]
        proj = {"share_a": 0.491, "lo": 0.482, "hi": 0.501, "prob_a": 0.08}
        with tempfile.TemporaryDirectory() as d:
            cases = {"cheio": (10.0, 0.46, proj, hist), "sem_hist": (0.4, 0.44, proj, []),
                     "sem_dados": (0.0, None, proj, []), "sem_projecao": (5.0, 0.5, None, hist),
                     "certeza": (90.0, 0.48, {"share_a": 0.47, "lo": 0.468, "hi": 0.472, "prob_a": 0.0}, hist)}
            sizes = {}
            for name, (pst, obs, pr, h) in cases.items():
                p = G.render(os.path.join(d, f"{name}.png"), datetime(2026, 10, 25, 19, 0), pst, obs, pr, h)
                with Image.open(p) as im:
                    sizes[name] = im.size
                    self.assertEqual(im.size[0], 1080)
            self.assertGreater(sizes["cheio"][1], sizes["sem_hist"][1])  # a seção "a noite" só entra com histórico


if __name__ == "__main__":
    unittest.main()
