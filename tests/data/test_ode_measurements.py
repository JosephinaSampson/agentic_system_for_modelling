import csv
import io
import tempfile
import unittest
from pathlib import Path

from scripts.ode import measurements

HEADER = ",".join(measurements.COLUMNS)
ROW = "M1,pR,ligand,0,min,,,1.0,a.u.,0.1,sd,3,fit,s1,Fig. 1A"


def table(*rows, header=HEADER):
    return "\ufeff" + "\n".join([header, *rows]) + "\n"


class MeasurementTableTests(unittest.TestCase):
    def read(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "m.csv"
        path.write_text(text, encoding="utf-8")
        return measurements.read(path)

    def test_columns_are_the_documented_ones(self):
        self.assertEqual(measurements.COLUMNS, (
            "measurement_id", "readout", "condition", "time", "time_unit", "dose", "dose_unit", "value",
            "value_unit", "error", "error_kind", "n", "use", "source", "locator"))

    def test_read_parses_numbers_and_empties(self):
        (row,) = self.read(table(ROW))
        self.assertEqual((row["time"], row["value"], row["error"], row["n"]), (0.0, 1.0, 0.1, 3))
        self.assertIsInstance(row["n"], int)
        self.assertIsNone(row["dose"])
        bare = self.read(table("M2,pR,c,1,min,2,uM,1,a.u.,,,,holdout,s1,Fig. 1"))[0]
        self.assertEqual((bare["dose"], bare["error"], bare["n"]), (2.0, None, None))

    def test_read_refuses_missing_column_and_bad_number(self):
        with self.assertRaisesRegex(ValueError, "columns"):
            self.read(table("M1", header="measurement_id"))
        with self.assertRaisesRegex(ValueError, "value"):
            self.read(table(ROW.replace(",1.0,a.u.", ",abc,a.u.")))

    def check(self, *rows, sources=("s1",)):
        return measurements.check(self.read(table(*rows)), set(sources))

    def test_check_passes_good_row(self):
        self.assertEqual(self.check(ROW), [])

    def test_check_flags_units_values_enums_sources(self):
        self.assertTrue(any("value_unit" in e for e in self.check(ROW.replace("a.u.", ""))))
        self.assertTrue(any("time_unit" in e for e in self.check(ROW.replace(",min,", ",,"))))
        self.assertTrue(any("dose_unit" in e for e in self.check("M1,pR,c,0,min,2,,1,a.u.,,,,fit,s1,Fig. 1")))
        self.assertTrue(any("finite" in e for e in self.check(ROW.replace(",1.0,", ",nan,"))))
        self.assertTrue(any("use" in e for e in self.check(ROW.replace(",fit,", ",train,"))))
        self.assertTrue(any("source" in e for e in self.check(ROW, sources=("other",))))
        self.assertTrue(any("locator" in e for e in self.check(ROW.replace("Fig. 1A", ""))))
        self.assertTrue(any("error_kind" in e for e in self.check(ROW.replace(",sd,", ",,"))))
        self.assertTrue(any("duplicate" in e for e in self.check(ROW, ROW)))
        self.assertTrue(any("no rows" in e for e in self.check()))


if __name__ == "__main__":
    unittest.main()
