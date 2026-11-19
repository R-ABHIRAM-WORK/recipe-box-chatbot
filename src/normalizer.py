"""Deterministic, conservative ingredient normalizer, quantity parser, and step counter.

Used BOTH during offline database ingestion (`build_db.py`) and at query time
to normalize user inputs with the exact same rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
from typing import Optional

# ---------------------------------------------------------------------------
# 1. Unicode fractions & numeric parsing
# ---------------------------------------------------------------------------
UNICODE_FRACTIONS: dict[str, str] = {
    "¼": " 1/4",
    "½": " 1/2",
    "¾": " 3/4",
    "⅐": " 1/7",
    "⅑": " 1/9",
    "⅒": " 1/10",
    "⅓": " 1/3",
    "⅔": " 2/3",
    "⅕": " 1/5",
    "⅖": " 2/5",
    "⅗": " 3/5",
    "⅘": " 4/5",
    "⅙": " 1/6",
    "⅚": " 5/6",
    "⅛": " 1/8",
    "⅜": " 3/8",
    "⅝": " 5/8",
    "⅞": " 7/8",
}

NUMBER_WORDS: dict[str, str] = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "dozen": "12",
}

# ---------------------------------------------------------------------------
# 2. Units and safe base-unit conversions (never guesses weight <-> volume!)
# ---------------------------------------------------------------------------
# Maps unit token -> (normalized_unit, unit_type, factor_to_base)
# Base units: volume -> ml, weight -> g, count -> count
UNIT_TABLE: dict[str, tuple[str, str, Optional[float]]] = {
    # Volume (-> ml)
    "tsp": ("tsp", "volume", 4.92892),
    "tsp.": ("tsp", "volume", 4.92892),
    "tsps": ("tsp", "volume", 4.92892),
    "teaspoon": ("tsp", "volume", 4.92892),
    "teaspoons": ("tsp", "volume", 4.92892),
    "t": ("tsp", "volume", 4.92892),
    "tbsp": ("tbsp", "volume", 14.7868),
    "tbsp.": ("tbsp", "volume", 14.7868),
    "tbsps": ("tbsp", "volume", 14.7868),
    "tbs": ("tbsp", "volume", 14.7868),
    "tablespoon": ("tbsp", "volume", 14.7868),
    "tablespoons": ("tbsp", "volume", 14.7868),
    "cup": ("cup", "volume", 236.588),
    "cups": ("cup", "volume", 236.588),
    "c": ("cup", "volume", 236.588),
    "c.": ("cup", "volume", 236.588),
    "ml": ("ml", "volume", 1.0),
    "milliliter": ("ml", "volume", 1.0),
    "milliliters": ("ml", "volume", 1.0),
    "millilitre": ("ml", "volume", 1.0),
    "millilitres": ("ml", "volume", 1.0),
    "l": ("l", "volume", 1000.0),
    "liter": ("l", "volume", 1000.0),
    "liters": ("l", "volume", 1000.0),
    "litre": ("l", "volume", 1000.0),
    "litres": ("l", "volume", 1000.0),
    "fl oz": ("fl_oz", "volume", 29.5735),
    "fluid ounce": ("fl_oz", "volume", 29.5735),
    "fluid ounces": ("fl_oz", "volume", 29.5735),
    "pint": ("pint", "volume", 473.176),
    "pints": ("pint", "volume", 473.176),
    "pt": ("pint", "volume", 473.176),
    "pt.": ("pint", "volume", 473.176),
    "quart": ("quart", "volume", 946.353),
    "quarts": ("quart", "volume", 946.353),
    "qt": ("quart", "volume", 946.353),
    "qt.": ("quart", "volume", 946.353),
    "gallon": ("gallon", "volume", 3785.41),
    "gallons": ("gallon", "volume", 3785.41),
    "gal": ("gallon", "volume", 3785.41),
    "gal.": ("gallon", "volume", 3785.41),
    "ml.": ("ml", "volume", 1.0),
    "l.": ("l", "volume", 1000.0),
    # Weight (-> g)
    "g": ("g", "weight", 1.0),
    "g.": ("g", "weight", 1.0),
    "gram": ("g", "weight", 1.0),
    "grams": ("g", "weight", 1.0),
    "kg": ("kg", "weight", 1000.0),
    "kg.": ("kg", "weight", 1000.0),
    "kilogram": ("kg", "weight", 1000.0),
    "kilograms": ("kg", "weight", 1000.0),
    "oz": ("oz", "weight", 28.3495),
    "oz.": ("oz", "weight", 28.3495),
    "ounce": ("oz", "weight", 28.3495),
    "ounces": ("oz", "weight", 28.3495),
    "lb": ("lb", "weight", 453.592),
    "lb.": ("lb", "weight", 453.592),
    "lbs": ("lb", "weight", 453.592),
    "lbs.": ("lb", "weight", 453.592),
    "pound": ("lb", "weight", 453.592),
    "pounds": ("lb", "weight", 453.592),
    # Container / discrete units (other — not cross-convertible to g/ml)
    "can": ("can", "other", None),
    "cans": ("can", "other", None),
    "jar": ("jar", "other", None),
    "jars": ("jar", "other", None),
    "bottle": ("bottle", "other", None),
    "bottles": ("bottle", "other", None),
    "package": ("package", "other", None),
    "packages": ("package", "other", None),
    "pkg": ("package", "other", None),
    "pkg.": ("package", "other", None),
    "packet": ("packet", "other", None),
    "packets": ("packet", "other", None),
    "box": ("box", "other", None),
    "boxes": ("box", "other", None),
    "bag": ("bag", "other", None),
    "bags": ("bag", "other", None),
    "container": ("container", "other", None),
    "containers": ("container", "other", None),
    "bunch": ("bunch", "other", None),
    "bunches": ("bunch", "other", None),
    "sprig": ("sprig", "other", None),
    "sprigs": ("sprig", "other", None),
    "stalk": ("stalk", "other", None),
    "stalks": ("stalk", "other", None),
    "rib": ("rib", "other", None),
    "ribs": ("rib", "other", None),
    "head": ("head", "other", None),
    "heads": ("head", "other", None),
    "clove": ("clove", "other", None),
    "cloves": ("clove", "other", None),
    "slice": ("slice", "other", None),
    "slices": ("slice", "other", None),
    "strip": ("strip", "other", None),
    "strips": ("strip", "other", None),
    "piece": ("piece", "other", None),
    "pieces": ("piece", "other", None),
    "stick": ("stick", "other", None),
    "sticks": ("stick", "other", None),
    "pinch": ("pinch", "other", None),
    "pinches": ("pinch", "other", None),
    "dash": ("dash", "other", None),
    "dashes": ("dash", "other", None),
    "drop": ("drop", "other", None),
    "drops": ("drop", "other", None),
    "handful": ("handful", "other", None),
    "handfuls": ("handful", "other", None),
    "ear": ("ear", "other", None),
    "ears": ("ear", "other", None),
    "leaf": ("leaf", "other", None),
    "leaves": ("leaf", "other", None),
    "sheet": ("sheet", "other", None),
    "sheets": ("sheet", "other", None),
    "loaf": ("loaf", "other", None),
    "loaves": ("loaf", "other", None),
    "wedge": ("wedge", "other", None),
    "wedges": ("wedge", "other", None),
    "cube": ("cube", "other", None),
    "cubes": ("cube", "other", None),
    "scoop": ("scoop", "other", None),
    "scoops": ("scoop", "other", None),
    "envelope": ("envelope", "other", None),
    "envelopes": ("envelope", "other", None),
    "inch": ("inch", "other", None),
    "inches": ("inch", "other", None),
    "link": ("link", "other", None),
    "links": ("link", "other", None),
    "fillet": ("fillet", "other", None),
    "fillets": ("fillet", "other", None),
    "filet": ("fillet", "other", None),
    "filets": ("fillet", "other", None),
    "carton": ("carton", "other", None),
    "cartons": ("carton", "other", None),
    "tub": ("tub", "other", None),
    "tubs": ("tub", "other", None),
    "jug": ("jug", "other", None),
    "jugs": ("jug", "other", None),
    "pouch": ("pouch", "other", None),
    "pouches": ("pouch", "other", None),
    "bar": ("bar", "other", None),
    "bars": ("bar", "other", None),
    "block": ("block", "other", None),
    "blocks": ("block", "other", None),
    "roll": ("roll", "other", None),
    "rolls": ("roll", "other", None),
    "slab": ("slab", "other", None),
    "slabs": ("slab", "other", None),
    "splash": ("splash", "other", None),
    "splashes": ("splash", "other", None),
}

# ---------------------------------------------------------------------------
# 3. Safe noise words (only non-identity size/prep/quality words!)
#    NEVER strip variety/form words like red, green, long, paste, sauce,
#    powder, milk, butter, coconut, almond, peanut, cream, sweet, etc.
# ---------------------------------------------------------------------------
NOISE_WORDS: set[str] = {
    # Size / temperature / freshness / quality
    "fresh", "freshly", "organic", "natural", "pure", "real", "good",
    "quality", "good-quality", "high-quality", "best", "fine", "finely",
    "coarse", "coarsely", "roughly", "thinly", "thickly", "lightly",
    "heaping", "level", "packed", "loosely", "firmly", "generous", "scant",
    "large", "extra-large", "medium", "medium-large", "medium-sized",
    "small", "tiny", "jumbo", "whole", "regular", "plain", "standard",
    "unsalted", "salted", "cold", "chilled", "warm", "hot", "boiling",
    "lukewarm", "room-temperature", "ripe", "unripe", "raw", "lean",
    "extra-lean", "boneless", "skinless", "bone-in", "skin-on",
    "pitted", "unpitted", "peeled", "unpeeled", "seeded", "unseeded",
    "deseeded", "cored", "trimmed", "cleaned", "washed", "rinsed",
    "drained", "undrained", "thawed", "defrosted", "softened", "melted",
    "beaten", "whisked", "scrambled", "sifted", "toasted", "untoasted",
    "blanched", "hulled", "shelled", "unshelled", "deveined", "butterflied",
    # Cutting / prep words
    "chopped", "diced", "minced", "sliced", "shredded", "grated",
    "crushed", "crumbled", "cubed", "halved", "quartered", "julienned",
    "mashed", "pureed", "puréed", "pressed", "squeezed", "juiced",
    "zested", "torn", "broken", "cut", "trimmed", "scrubbed", "snapped",
    "stemmed", "destemmed", "flaked", "pounded", "flattened", "scored",
    "divided", "separated", "reserved", "optional", "needed", "desired",
    "preferred", "taste", "serving", "garnish", "garnishing", "dusting",
    "greasing", "frying", "drizzling", "brushing", "sprinkling", "coating",
    "about", "approximately", "approx", "roughly", "around", "nearly",
    "plus", "more", "extra", "additional", "remaining", "leftover",
    "prepared", "store-bought", "storebought", "homemade", "commercial",
    "cooked", "uncooked", "pre-cooked", "precooked", "instant",
    "refrigerated", "canned", "jarred", "bottled", "frozen", "boxed",
    "thick-cut", "thin-cut", "center-cut", "bite-sized", "bite-size",
    "cracked", "ground",  # note: protected for "ground beef", "ground turkey", etc.
    "of", "or", "and", "the", "a", "an", "for", "to", "in", "with", "into",
    "from", "on", "at", "by", "as", "if", "per",
}

# Phrases where words like "ground" or "whole" ARE part of the culinary identity
KEEP_LEADING_MODIFIER_WITH: set[str] = {
    "ground beef", "ground turkey", "ground pork", "ground chicken",
    "ground lamb", "ground veal", "ground bison", "ground sausage",
    "ground meat", "ground cinnamon", "ground cumin", "ground ginger",
    "ground nutmeg", "ground cloves", "ground coriander", "ground cardamom",
    "ground turmeric", "ground mustard", "ground allspice", "ground cayenne",
    "ground flaxseed", "ground almond", "ground almonds",
    "whole milk", "whole wheat flour", "whole-wheat flour", "whole grain mustard",
    "whole grain", "crushed tomato", "crushed tomatoes", "diced tomato", "diced tomatoes",
    "stewed tomato", "stewed tomatoes", "crushed red pepper",
    "sliced almond", "sliced almonds", "slivered almond", "slivered almonds",
    "shredded coconut", "flaked coconut", "condensed milk", "evaporated milk",
    "whipped cream", "whipping cream", "heavy cream", "sour cream",
    "powdered sugar", "granulated sugar", "brown sugar", "dark brown sugar",
    "light brown sugar", "raw sugar", "turbinado sugar",
    "smoked paprika", "smoked salmon", "smoked bacon", "roasted red pepper",
    "sun-dried tomato", "sun-dried tomatoes", "dried cranberry", "dried cranberries",
    "dried apricot", "dried apricots", "dried cherry", "dried cherries",
    "dried thyme", "dried oregano", "dried basil", "dried rosemary",
    "sweet potato", "sweet potatoes", "sweet onion", "sweetened condensed milk",
    "unsweetened cocoa powder", "cocoa powder", "baking powder", "baking soda",
    "active dry yeast", "instant yeast", "dry yeast",
    "long brinjal", "green onion", "red onion", "yellow onion", "white onion",
    "pearl onion", "vidalia onion", "shallot",
    "black bean", "black beans", "kidney bean", "kidney beans",
    "pinto bean", "pinto beans", "white bean", "white beans",
    "green bean", "green beans", "garbanzo bean", "garbanzo beans",
    "black pepper", "white pepper", "cayenne pepper", "red pepper flake",
    "red pepper flakes", "bell pepper", "green bell pepper", "red bell pepper",
    "yellow bell pepper", "jalapeno pepper", "chili pepper",
    "olive oil", "vegetable oil", "canola oil", "sesame oil", "coconut oil",
    "peanut oil", "sunflower oil", "avocado oil", "corn oil",
    "soy sauce", "fish sauce", "worcestershire sauce", "hot sauce",
    "tomato sauce", "tomato paste", "bbq sauce", "barbecue sauce", "oyster sauce",
    "hoisin sauce", "teriyaki sauce", "enchilada sauce", "marinara sauce",
    "alfredo sauce", "pesto", "salsa",
    "peanut butter", "almond butter", "cashew butter", "sunflower butter",
    "apple butter", "cocoa butter", "buttermilk",
    "coconut milk", "almond milk", "soy milk", "oat milk", "rice milk",
    "coconut cream", "cream cheese", "cream of tartar", "ice cream",
    "egg white", "egg whites", "egg yolk", "egg yolks", "egg noodle", "egg noodles",
    "chicken broth", "chicken stock", "beef broth", "beef stock",
    "vegetable broth", "vegetable stock",
    "all-purpose flour", "bread flour", "cake flour", "pastry flour",
    "almond flour", "coconut flour", "rice flour", "cornmeal", "cornstarch",
    "bread crumb", "bread crumbs", "panko", "panko bread crumb",
    "parmesan cheese", "cheddar cheese", "mozzarella cheese", "feta cheese",
    "goat cheese", "ricotta cheese", "swiss cheese", "monterey jack cheese",
    "cottage cheese", "blue cheese", "provolone cheese", "pecorino romano",
    "romano cheese", "gruyere cheese", "fontina cheese", "asiago cheese",
    "american cheese", "jack cheese", "colby cheese", "brie",
    "greek yogurt", "plain yogurt", "yogurt",
    "white wine", "red wine", "dry white wine", "dry red wine",
    "rice vinegar", "apple cider vinegar", "balsamic vinegar",
    "red wine vinegar", "white wine vinegar", "distilled white vinegar", "cider vinegar",
    "maple syrup", "corn syrup", "simple syrup", "honey", "molasses", "agave",
    "dark chocolate", "milk chocolate", "white chocolate", "semisweet chocolate",
    "bittersweet chocolate", "chocolate chip", "chocolate chips",
    "vanilla extract", "almond extract", "lemon juice", "lime juice", "orange juice",
    "lemon zest", "lime zest", "orange zest",
    "chicken breast", "chicken thigh", "chicken wing", "chicken drumstick",
    "pork chop", "pork tenderloin", "pork shoulder", "pork belly",
    "beef chuck", "skirt steak", "flank steak", "sirloin steak", "ribeye steak",
}

# Exact, well-justified synonyms only (never collapses distinct culinary products!)
SYNONYM_MAP: dict[str, str] = {
    # Alliums
    "scallion": "green onion",
    "scallions": "green onion",
    "spring onion": "green onion",
    "spring onions": "green onion",
    "green onions": "green onion",
    # Salt / Pepper
    "kosher salt": "salt",
    "sea salt": "salt",
    "table salt": "salt",
    "fine salt": "salt",
    "coarse salt": "salt",
    "flaky salt": "salt",
    "flaky sea salt": "salt",
    "fine sea salt": "salt",
    "coarse kosher salt": "salt",
    "freshly ground black pepper": "black pepper",
    "freshly cracked black pepper": "black pepper",
    "cracked black pepper": "black pepper",
    "ground black pepper": "black pepper",
    "black peppercorns": "black pepper",
    "peppercorns": "black pepper",
    # Oils
    "extra virgin olive oil": "olive oil",
    "extra-virgin olive oil": "olive oil",
    "evoo": "olive oil",
    "virgin olive oil": "olive oil",
    "light olive oil": "olive oil",
    "pure olive oil": "olive oil",
    # Sugars
    "white sugar": "sugar",
    "granulated sugar": "sugar",
    "granulated white sugar": "sugar",
    "caster sugar": "superfine sugar",
    "castor sugar": "superfine sugar",
    "confectioners sugar": "powdered sugar",
    "confectioners' sugar": "powdered sugar",
    "confectioner sugar": "powdered sugar",
    "icing sugar": "powdered sugar",
    # Flours
    "all purpose flour": "all-purpose flour",
    "ap flour": "all-purpose flour",
    "plain flour": "all-purpose flour",
    "unbleached all-purpose flour": "all-purpose flour",
    "unbleached all purpose flour": "all-purpose flour",
    "bleached all-purpose flour": "all-purpose flour",
    "whole wheat flour": "whole-wheat flour",
    # Dairy
    "heavy whipping cream": "heavy cream",
    "whipping cream": "heavy cream",
    "double cream": "heavy cream",
    "parmigiano reggiano": "parmesan cheese",
    "parmigiano-reggiano": "parmesan cheese",
    "parmesan": "parmesan cheese",
    "cheddar": "cheddar cheese",
    "sharp cheddar": "cheddar cheese",
    "sharp cheddar cheese": "cheddar cheese",
    "mild cheddar": "cheddar cheese",
    "mozzarella": "mozzarella cheese",
    "feta": "feta cheese",
    "ricotta": "ricotta cheese",
    "monterey jack": "monterey jack cheese",
    # Pantry staples
    "corn starch": "cornstarch",
    "bicarbonate of soda": "baking soda",
    "garbanzo bean": "chickpea",
    "garbanzo beans": "chickpea",
    "chickpeas": "chickpea",
    "cilantro leaves": "cilantro",
    "coriander leaves": "cilantro",
    "italian parsley": "parsley",
    "flat-leaf parsley": "parsley",
    "flat leaf parsley": "parsley",
    "curly parsley": "parsley",
    "garlic clove": "garlic",
    "garlic cloves": "garlic",
    "clove garlic": "garlic",
    "cloves garlic": "garlic",
    "aubergine": "eggplant",
    "brinjal": "eggplant",
    "courgette": "zucchini",
    "capsicum": "bell pepper",
    "breadcrumbs": "bread crumb",
    "bread crumbs": "bread crumb",
    "panko breadcrumbs": "panko",
    "panko bread crumbs": "panko",
    "ketchup": "ketchup",
    "catsup": "ketchup",
    "tamari": "soy sauce",
    "low-sodium soy sauce": "soy sauce",
    "low sodium soy sauce": "soy sauce",
    "reduced-sodium soy sauce": "soy sauce",
    "chicken breast half": "chicken breast",
    "chicken breast halves": "chicken breast",
    "chicken breasts": "chicken breast",
    "chicken thighs": "chicken thigh",
    "penne pasta": "penne",
    "penne rigate": "penne",
    "spaghetti pasta": "spaghetti",
    "elbow macaroni": "macaroni",
    "crushed red pepper flakes": "red pepper flake",
    "red pepper flakes": "red pepper flake",
    "hot red pepper flakes": "red pepper flake",
    "chili flakes": "red pepper flake",
    "chile flakes": "red pepper flake",
    "cayenne": "cayenne pepper",
    "ground cayenne": "cayenne pepper",
    "ground cayenne pepper": "cayenne pepper",
    "pure vanilla extract": "vanilla extract",
    "vanilla": "vanilla extract",
    "unsweetened cocoa": "cocoa powder",
    "unsweetened cocoa powder": "cocoa powder",
    "dutch-process cocoa powder": "cocoa powder",
    "roma tomato": "plum tomato",
    "roma tomatoes": "plum tomato",
    "plum tomatoes": "plum tomato",
    "cherry tomatoes": "cherry tomato",
    "grape tomatoes": "grape tomato",
    "yellow onions": "yellow onion",
    "red onions": "red onion",
    "white onions": "white onion",
    "sweet onions": "sweet onion",
    "vidalia onions": "sweet onion",
}

# Irregular singularizations
IRREGULAR_SINGULARS: dict[str, str] = {
    "tomatoes": "tomato",
    "potatoes": "potato",
    "cherries": "cherry",
    "berries": "berry",
    "strawberries": "strawberry",
    "blueberries": "blueberry",
    "raspberries": "raspberry",
    "blackberries": "blackberry",
    "cranberries": "cranberry",
    "anchovies": "anchovy",
    "halves": "half",
    "leaves": "leaf",
    "loaves": "loaf",
    "cloves": "clove",
    "peaches": "peach",
    "radishes": "radish",
    "squashes": "squash",
    "chiles": "chile",
    "chilies": "chili",
    "chillies": "chili",
    "jalapenos": "jalapeno",
    "jalapeños": "jalapeno",
    "avocados": "avocado",
    "mangoes": "mango",
    "mangos": "mango",
    "eggs": "egg",
    "onions": "onion",
    "carrots": "carrot",
    "mushrooms": "mushroom",
    "peppers": "pepper",
    "lemons": "lemon",
    "limes": "lime",
    "oranges": "orange",
    "apples": "apple",
    "bananas": "banana",
    "pears": "pear",
    "plums": "plum",
    "grapes": "grape",
    "olives": "olive",
    "capers": "caper",
    "scallions": "scallion",
    "shallots": "shallot",
    "leeks": "leek",
    "chives": "chive",
    "peas": "pea",
    "beans": "bean",
    "lentils": "lentil",
    "oats": "oat",
    "almonds": "almond",
    "walnuts": "walnut",
    "pecans": "pecan",
    "peanuts": "peanut",
    "cashews": "cashew",
    "pistachios": "pistachio",
    "hazelnuts": "hazelnut",
    "raisins": "raisin",
    "dates": "date",
    "figs": "fig",
    "apricots": "apricot",
    "noodles": "noodle",
    "tortillas": "tortilla",
    "crackers": "cracker",
    "chips": "chip",
    "crumbs": "crumb",
    "flakes": "flake",
    "seeds": "seed",
    "spices": "spice",
    "herbs": "herb",
    "whites": "white",
    "yolks": "yolk",
    "breasts": "breast",
    "thighs": "thigh",
    "wings": "wing",
    "chops": "chop",
    "steaks": "steak",
    "shrimps": "shrimp",
    "prawns": "prawn",
    "scallops": "scallop",
    "clams": "clam",
    "mussels": "mussel",
    "oysters": "oyster",
    "brinjals": "brinjal",
    "eggplants": "eggplant",
    "zucchinis": "zucchini",
    "cucumbers": "cucumber",
}

# Non-dairy false positives that contain words like "milk", "butter", or "cream"
NON_DAIRY_EXCEPTIONS: tuple[str, ...] = (
    "coconut milk", "almond milk", "soy milk", "oat milk", "rice milk",
    "cashew milk", "hemp milk", "coconut cream", "peanut butter",
    "almond butter", "cashew butter", "sunflower butter", "apple butter",
    "cocoa butter", "shea butter", "nut butter", "seed butter",
    "cream of tartar", "dairy-free", "dairy free", "vegan butter",
    "vegan cheese", "vegan cream", "non-dairy", "nondairy", "margarine",
    "butternut", "butterbean", "butter bean", "butter lettuce",
)

DAIRY_KEYWORDS: tuple[str, ...] = (
    "milk", "butter", "cream", "cheese", "yogurt", "yoghurt", "cheddar",
    "parmesan", "parmigiano", "mozzarella", "ricotta", "feta", "brie",
    "gouda", "gruyere", "gruyère", "pecorino", "romano", "asiago",
    "provolone", "mascarpone", "gorgonzola", "roquefort", "camembert",
    "fontina", "havarti", "muenster", "monterey jack", "paneer", "ghee",
    "buttermilk", "half-and-half", "half and half", "whey", "casein",
    "creme fraiche", "crème fraîche", "kefir", "custard", "ice cream",
    "velveeta", "queso", "cotija", "manchego", "burrata", "halloumi",
    "labneh", "quark", "skyr", "sour cream", "condensed milk", "evaporated milk",
)

TREE_NUT_KEYWORDS: tuple[str, ...] = (
    "almond", "walnut", "pecan", "cashew", "pistachio", "hazelnut",
    "macadamia", "pine nut", "brazil nut", "chestnut", "marzipan", "praline",
)

SHELLFISH_KEYWORDS: tuple[str, ...] = (
    "shrimp", "prawn", "crab", "lobster", "crayfish", "crawfish",
    "clam", "mussel", "oyster", "scallop", "squid", "calamari", "octopus",
)

# Regex patterns compiled once for speed across ~1M ingredient rows
_RE_ADVERTISEMENT = re.compile(r"\s*ADVERTISEMENT\s*", re.IGNORECASE)
_RE_SECTION_HEADER = re.compile(r"^\s*(?:for\s+the\s+|for\s+|to\s+serve|assembly|garnish|topping|glaze|sauce|filling|crust|frosting|dressing|marinade|dough|batter)[^,]{0,40}:\s*$", re.IGNORECASE)
_RE_COLON_ONLY_HEADER = re.compile(r"^[A-Za-z\s\-/&'()]{2,45}:\s*$")
_RE_PARENS = re.compile(r"\([^)]*\)")
_RE_PAREN_QTY_UNIT = re.compile(
    r"\(\s*(\d+(?:\.\d+)?(?:\s+\d+/\d+|\s*/\s*\d+)?)\s*(oz\.?|ounces?|lb\.?|lbs\.?|pounds?|g|grams?|kg|ml|l|liters?|cups?|inch|inches)\s*\)",
    re.IGNORECASE,
)
_RE_LEADING_QTY = re.compile(
    r"^\s*(?:about|approx\.?|approximately|roughly|nearly|scant|generous|heaping)?\s*"
    r"(?P<q1>\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)"
    r"(?:\s*(?:-|–|—|to|or)\s*(?P<q2>\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?))?"
    r"\s*",
    re.IGNORECASE,
)
_RE_OPTIONAL = re.compile(r"\b(?:optional|for garnish|for serving|to garnish|if desired)\b", re.IGNORECASE)
_RE_SALT_AND_PEPPER = re.compile(
    r"^(?:kosher\s+|sea\s+|fine\s+|coarse\s+)?salt\s+and\s+(?:fresh(?:ly)?\s+)?(?:ground\s+|cracked\s+)?(?:black\s+|white\s+)?pepper(?:\s*,.*)?$",
    re.IGNORECASE,
)
_RE_MULTISPACE = re.compile(r"\s+")
_RE_NON_TEXT = re.compile(r"[^a-z0-9\s\-']")
_RE_BOILERPLATE_STEP = re.compile(
    r"^\s*(?:watch how to make this recipe\.?|copyright.*|photographs? by.*|recipe courtesy of.*)\s*$",
    re.IGNORECASE,
)
_RE_STEP_PREFIX = re.compile(r"^\s*(?:step\s+\d+[:.)\-]?\s*|\d+[.)]\s+)", re.IGNORECASE)
_RE_ABBREV = re.compile(r"\b(?:e\.g|i\.e|approx|min|mins|hr|hrs|oz|lb|lbs|tbsp|tsp|deg|temp|vs|st|mr|mrs|dr)\.$", re.IGNORECASE)
_RE_DIMENSION = re.compile(r"\b\d+(?:\s*[x/]\s*\d+|\.\d+)?\s*-?\s*(?:inch(?:es)?|cm|mm)\b")
_RE_TRAILING_NOISE = re.compile(r"\b(?:to taste|as needed|for serving|for garnish|if desired|optional)\b.*$")
_RE_SUCH_AS = re.compile(r"\bsuch as\b.*$")
_RE_LEADING_OF = re.compile(r"^(?:of\s+a\s+|of\s+an\s+|of\s+)", re.IGNORECASE)
_RE_LEADING_BULLET_OR = re.compile(r"^\s*(?:[\*•\-–—]+\s*|or\s+)+", re.IGNORECASE)
_RE_ALT_OR_CLAUSE = re.compile(r"\s+\bor\b\s+(?=\d|\(|a\s+|an\s+|any\s+|other\s+|about\s+|approx|[a-z]+\s+[a-z]+)", re.IGNORECASE)
_RE_NON_ING_EQUIPMENT_NOTE = re.compile(
    r"^\s*\**\s*(?:special\s+equipment|equipment|cook'?s?\s+note|chef'?s?\s+note|note|notes|accompaniment|variation|serving\s+suggestion)\s*:",
    re.IGNORECASE,
)
_RE_EPICURIOUS_FOOTNOTE = re.compile(
    r"^\s*\*+\s*(?:available\b|found\b|sold\b|a\s+|an\s+|the\s+|this\b|these\b|can\s+be\b|if\b|we\b|you\b|for\b|small\s+and\b|also\b|sometimes\b)",
    re.IGNORECASE,
)
_RE_HTML_FRAGMENT = re.compile(r"^\s*/?(?:em|strong|br|p|div|span|b|i|li|ul|ol)\s*$", re.IGNORECASE)
_RE_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_RE_JUNK_TITLE = re.compile(r"^(?:tst[a-z]*|qa|test(?:\s+recipe)?|testing|temp|asdf+|ttt+|xxx+)$", re.IGNORECASE)
_RE_JUNK_INSTRUCTIONS = re.compile(r"^\s*(?:method\s+method|test\s+test|asdf+|lorem\s+ipsum)", re.IGNORECASE)

# Precompiled broad-family patterns (1 regex per family instead of looping!)
_RE_DAIRY = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in DAIRY_KEYWORDS) + r")\b")
_RE_SHELLFISH = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in SHELLFISH_KEYWORDS) + r")\b")
_RE_SOY = re.compile(r"\b(?:soy sauce|tofu|edamame|tempeh|miso|soybean|soy milk)\b")
_RE_EGG = re.compile(r"\begg(?:s|\s+white|\s+yolk)?\b")
_RE_TOMATO = re.compile(r"\b(?:tomato|tomatoes)\b")
_RE_ONION = re.compile(r"\bonion\b")
_RE_GARLIC = re.compile(r"\bgarlic\b")
_RE_EGGPLANT = re.compile(r"\b(?:eggplant|brinjal)\b")
_RE_POTATO = re.compile(r"\bpotato\b")
_RE_CHICKEN = re.compile(r"\bchicken\b")
_RE_BEEF = re.compile(r"\b(?:beef|steak|brisket|veal)\b")
_RE_PORK = re.compile(r"\b(?:pork|bacon|ham|prosciutto|pancetta)\b")
_RE_RICE = re.compile(r"\brice\b")
_RE_PASTA = re.compile(
    r"\b(?:pasta|penne|spaghetti|fettuccine|linguine|rigatoni|macaroni|fusilli|farfalle|orzo|lasagna|tagliatelle|ziti|ravioli|tortellini)\b"
)
_RE_FLOUR = re.compile(r"\bflour\b")
_RE_SUGAR = re.compile(r"\bsugar\b")


@dataclass
class ParsedIngredient:
    raw_text: str
    quantity: Optional[float]
    qty_max: Optional[float]
    unit: Optional[str]
    unit_type: str  # 'volume', 'weight', 'count', 'other', 'unspecified'
    quantity_base: Optional[float]  # ml, g, or count
    canonical_name: str
    broad_family: Optional[str]
    is_optional: bool
    prep_notes: str


def _parse_number(token: str) -> Optional[float]:
    token = token.strip()
    if not token:
        return None
    try:
        if " " in token and "/" in token:
            whole, frac = token.split(None, 1)
            num, den = frac.split("/", 1)
            den_f = float(den)
            return float(whole) + (float(num) / den_f) if den_f else None
        if "/" in token:
            num, den = token.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else None
        return float(token)
    except ValueError:
        return None


def _replace_unicode_fractions(text: str) -> str:
    for ch, rep in UNICODE_FRACTIONS.items():
        if ch in text:
            text = text.replace(ch, rep)
    return _RE_MULTISPACE.sub(" ", text).strip()


def _singularize_word(word: str) -> str:
    if word in IRREGULAR_SINGULARS:
        return IRREGULAR_SINGULARS[word]
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "zes", "ches", "shes")) and not word.endswith(("cheese", "molasses")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is", "as", "os", "ous", "grass", "cress", "hummus", "couscous", "molasses", "asparagus")):
        return word[:-1]
    return word


def _singularize_phrase(phrase: str) -> str:
    words = phrase.split()
    if not words:
        return ""
    # Singularize the final head word (and keep compound modifiers intact)
    words[-1] = _singularize_word(words[-1])
    return " ".join(words)


@lru_cache(maxsize=65536)
def classify_broad_family(canonical: str) -> Optional[str]:
    """Tag high-value culinary / allergen families safely without collapsing canonical_name."""
    text = canonical.lower()
    if not text:
        return None

    # Allergen families (checked carefully against false positives)
    if "peanut" in text:
        return "peanut"
    if any(k in text for k in TREE_NUT_KEYWORDS):
        return "tree_nut"
    if not any(exc in text for exc in NON_DAIRY_EXCEPTIONS):
        if _RE_DAIRY.search(text):
            return "dairy"
    if _RE_EGG.search(text) and "eggplant" not in text and "egg noodle" not in text:
        return "egg"
    if _RE_SHELLFISH.search(text):
        return "shellfish"
    if _RE_SOY.search(text):
        return "soy"

    # Common produce / staple families for broad stats & queries
    if _RE_TOMATO.search(text):
        return "tomato"
    if _RE_ONION.search(text) or text in ("shallot", "leek", "chive"):
        return "onion"
    if _RE_GARLIC.search(text):
        return "garlic"
    if _RE_EGGPLANT.search(text):
        return "eggplant"
    if _RE_POTATO.search(text) and "sweet potato" not in text:
        return "potato"
    if _RE_CHICKEN.search(text):
        return "chicken"
    if _RE_BEEF.search(text):
        return "beef"
    if _RE_PORK.search(text):
        return "pork"
    if _RE_RICE.search(text) and "rice vinegar" not in text and "rice wine" not in text and "rice milk" not in text:
        return "rice"
    if _RE_PASTA.search(text):
        return "pasta"
    if _RE_FLOUR.search(text):
        return "flour"
    if _RE_SUGAR.search(text):
        return "sugar"
    return None


@lru_cache(maxsize=65536)
def normalize_ingredient_name(text: str) -> str:
    """Normalize a raw ingredient description (without leading qty/unit) into a
    conservative, deterministic canonical name."""
    s = text.lower().strip()
    raw_has_dessert_base = any(
        k in s for k in ("ice cream", "gelato", "sorbet", "frozen yogurt", "pudding", "wafer", "bean", "pod")
    )
    s = _RE_PARENS.sub(" ", s)
    # Remove size/cut dimensions like "1/4-inch", "2x1-inch", "1/2 inch"
    s = _RE_DIMENSION.sub(" ", s)
    # Remove trailing "to taste", "as needed", etc.
    s = _RE_TRAILING_NOISE.sub("", s)
    # Strip trailing clauses after semicolon or "such as ..."
    s = s.split(";", 1)[0]
    s = _RE_SUCH_AS.sub("", s)
    # Trim alternative "or <qty/brand/other ingredient>" clause (take primary ingredient)
    m_or = _RE_ALT_OR_CLAUSE.search(s)
    if m_or and m_or.start() >= 3:
        left = s[: m_or.start()].strip(" ,-")
        # Guard against flavor lists like "vanilla, mint chip, or coffee ice cream"
        if not raw_has_dessert_base:
            s = left
    # Strip comma-separated prep notes ONLY if the left side has real (non-noise) tokens
    # AND we are not splitting a flavor list like "vanilla, mint chip, or coffee ice cream"
    if "," in s and not raw_has_dessert_base:
        segments = [seg.strip() for seg in s.split(",")]
        kept: list[str] = []
        for seg in segments:
            seg_clean = _RE_NON_TEXT.sub(" ", seg)
            seg_tokens = [w for w in seg_clean.split() if w and not w.isdigit()]
            kept.append(seg)
            has_core = any(w not in NOISE_WORDS for w in seg_tokens)
            if has_core:
                break
        s = " ".join(kept)
    s = _RE_NON_TEXT.sub(" ", s)
    s = _RE_MULTISPACE.sub(" ", s).strip(" -'")

    if not s:
        return ""

    # Check exact synonym / protected multi-word match BEFORE stripping noise
    if s in SYNONYM_MAP:
        if s == "vanilla" and raw_has_dessert_base:
            pass
        else:
            return SYNONYM_MAP[s]
    if s in KEEP_LEADING_MODIFIER_WITH:
        return _singularize_phrase(s)

    # Check if a protected phrase appears intact inside `s` (longest match first)
    words = s.split()
    cleaned_words: list[str] = []
    i = 0
    while i < len(words):
        # Try 3-word and 2-word protected phrases
        matched_phrase = None
        for span in (3, 2):
            if i + span <= len(words):
                cand = " ".join(words[i : i + span])
                if cand in KEEP_LEADING_MODIFIER_WITH or cand in SYNONYM_MAP:
                    matched_phrase = cand
                    i += span
                    break
        if matched_phrase:
            cleaned_words.extend(matched_phrase.split())
            continue

        w = words[i].strip("-'")
        i += 1
        if not w or w.isdigit() or w in NOISE_WORDS:
            continue
        # Drop stray leftover unit tokens when preceded by noise (e.g. "inch piece")
        if w in ("inch", "inches", "piece", "pieces", "chunks", "strips", "slices", "wedges", "rounds", "halves", "quarters"):
            continue
        cleaned_words.append(w)

    candidate = " ".join(cleaned_words).strip()
    if not candidate:
        # Fallback: keep original non-stopword tokens rather than returning empty
        fallback = [w for w in words if w not in ("of", "the", "a", "an", "to", "for", "and", "or")]
        candidate = " ".join(fallback[:4]).strip()

    if candidate in SYNONYM_MAP:
        if candidate == "vanilla" and raw_has_dessert_base:
            return candidate
        return SYNONYM_MAP[candidate]

    singular = _singularize_phrase(candidate)
    if singular in SYNONYM_MAP:
        if singular == "vanilla" and raw_has_dessert_base:
            return singular
        return SYNONYM_MAP[singular]

    # Reject garbage canonicals (pure numbers, single char, leftover unit-only tokens)
    if len(singular) <= 1 or singular.isdigit() or singular in ("em", "br", "oz", "ml", "lb", "tbsp", "tsp", "g", "kg"):
        return ""

    return singular


def clean_raw_ingredient_line(raw: str) -> str:
    """Remove Allrecipes ADVERTISEMENT artifacts and collapse whitespace."""
    cleaned = _RE_ADVERTISEMENT.sub(" ", raw or "")
    cleaned = _RE_MULTISPACE.sub(" ", cleaned).strip()
    return cleaned


def is_non_ingredient_line(cleaned: str) -> bool:
    """Return True for empty lines, pure ADVERTISEMENT lines, equipment/footnotes,
    HTML fragments, or section headers."""
    if not cleaned:
        return True
    low = cleaned.lower()
    if low in ("advertisement", "none", "n/a", "optional"):
        return True
    if _RE_HTML_FRAGMENT.match(cleaned):
        return True
    if _RE_NON_ING_EQUIPMENT_NOTE.match(cleaned):
        return True
    if _RE_EPICURIOUS_FOOTNOTE.match(cleaned):
        return True
    if _RE_SECTION_HEADER.match(cleaned) or _RE_COLON_ONLY_HEADER.match(cleaned):
        return True
    # Pure number / punctuation line like "1" or "4" or "---"
    if not any(ch.isalpha() for ch in cleaned):
        return True
    return False


def is_junk_recipe(title: str, instructions_raw: str) -> bool:
    """Conservatively flag obvious QA / placeholder / scraped test entries
    without touching legitimate short dish names (e.g. 'BLT', 'Pho', 'Dal', 'Poi')."""
    t = (title or "").strip()
    if not t:
        return True
    if _RE_JUNK_TITLE.match(t):
        return True
    if instructions_raw and _RE_JUNK_INSTRUCTIONS.match(instructions_raw):
        return True
    return False


def parse_ingredient_line(raw: str) -> list[ParsedIngredient]:
    """Parse one raw ingredient line into 0, 1, or 2 (for 'salt and pepper')
    ParsedIngredient objects."""
    cleaned = clean_raw_ingredient_line(raw)
    if is_non_ingredient_line(cleaned):
        return []

    is_opt = bool(_RE_OPTIONAL.search(cleaned))

    # Handle standard compound "Kosher salt and freshly ground black pepper"
    if _RE_SALT_AND_PEPPER.match(cleaned):
        return [
            ParsedIngredient(
                raw_text=cleaned,
                quantity=None,
                qty_max=None,
                unit=None,
                unit_type="unspecified",
                quantity_base=None,
                canonical_name="salt",
                broad_family=None,
                is_optional=is_opt,
                prep_notes="",
            ),
            ParsedIngredient(
                raw_text=cleaned,
                quantity=None,
                qty_max=None,
                unit=None,
                unit_type="unspecified",
                quantity_base=None,
                canonical_name="black pepper",
                broad_family=None,
                is_optional=is_opt,
                prep_notes="",
            ),
        ]

    work = _replace_unicode_fractions(cleaned)
    # Strip leading footnote asterisks, bullets, dashes, or stray leading "or "
    work = _RE_LEADING_BULLET_OR.sub("", work).strip()

    # Convert leading spelled-out numbers ("One large onion" -> "1 large onion")
    first_tok = work.split(None, 1)
    if first_tok and first_tok[0].lower() in NUMBER_WORDS:
        rest_str = first_tok[1] if len(first_tok) > 1 else ""
        work = f"{NUMBER_WORDS[first_tok[0].lower()]} {rest_str}".strip()

    # 1. Extract leading quantity (or range)
    qty_min: Optional[float] = None
    qty_max: Optional[float] = None
    m_qty = _RE_LEADING_QTY.match(work)
    if m_qty:
        qty_min = _parse_number(m_qty.group("q1"))
        qty_max = _parse_number(m_qty.group("q2")) if m_qty.group("q2") else qty_min
        work = work[m_qty.end() :].strip()

    # 2. Check for parenthetical package size right after qty: e.g. "(10.75 ounce) cans ..."
    paren_weight_qty: Optional[float] = None
    paren_unit_info: Optional[tuple[str, str, Optional[float]]] = None
    m_paren = _RE_PAREN_QTY_UNIT.match(work)
    if m_paren:
        p_qty = _parse_number(m_paren.group(1))
        p_unit_tok = m_paren.group(2).lower().rstrip(".")
        if p_qty is not None and p_unit_tok in UNIT_TABLE:
            paren_weight_qty = p_qty
            paren_unit_info = UNIT_TABLE[p_unit_tok]
        work = work[m_paren.end() :].strip()

    # 3. Extract leading unit token (check 2-word unit first like "fl oz", then 1-word)
    unit: Optional[str] = None
    unit_type = "unspecified"
    factor: Optional[float] = None

    tokens = work.split()
    if len(tokens) >= 2:
        two_word = f"{tokens[0].lower()} {tokens[1].lower().rstrip('.,')}"
        if two_word in UNIT_TABLE:
            unit, unit_type, factor = UNIT_TABLE[two_word]
            work = " ".join(tokens[2:])
            tokens = work.split()

    if unit is None and tokens:
        one_word = tokens[0].lower().rstrip(",;:")
        if one_word in UNIT_TABLE:
            # Guard: don't treat single-letter 'c' or 't' as cup/tsp unless followed by text and qty exists
            if one_word not in ("c", "t", "l") or qty_min is not None:
                unit, unit_type, factor = UNIT_TABLE[one_word]
                work = " ".join(tokens[1:])

    # Strip leading "of " after unit ("1 cup of milk" -> "milk")
    work = _RE_LEADING_OF.sub("", work).strip()

    # If we had a parenthetical weight/volume on cans/packages, e.g. "2 (10.75 oz) cans",
    # compute total base quantity (2 * 10.75 oz in grams) when unit is a container!
    quantity_base: Optional[float] = None
    if qty_min is not None:
        if paren_weight_qty is not None and paren_unit_info is not None and paren_unit_info[2] is not None and unit_type in ("other", "unspecified"):
            unit = paren_unit_info[0]
            unit_type = paren_unit_info[1]
            qty_min = round(qty_min * paren_weight_qty, 4)
            qty_max = round((qty_max or qty_min) * paren_weight_qty, 4) if qty_max is not None else qty_min
            quantity_base = round(qty_min * paren_unit_info[2], 3)
        elif factor is not None:
            quantity_base = round(qty_min * factor, 3)
        elif unit is None:
            # Bare count like "2 large eggs" or "1 onion"
            unit = "count"
            unit_type = "count"
            quantity_base = qty_min

    # Extract prep notes (text after first comma) for transparency
    prep_notes = ""
    if "," in work:
        prep_notes = work.split(",", 1)[1].strip()

    canonical = normalize_ingredient_name(work)
    if not canonical:
        canonical = normalize_ingredient_name(cleaned)
    if not canonical:
        return []

    family = classify_broad_family(canonical)

    return [
        ParsedIngredient(
            raw_text=cleaned,
            quantity=qty_min,
            qty_max=qty_max,
            unit=unit,
            unit_type=unit_type,
            quantity_base=quantity_base,
            canonical_name=canonical,
            broad_family=family,
            is_optional=is_opt,
            prep_notes=prep_notes,
        )
    ]


# ---------------------------------------------------------------------------
# 4. Deterministic instruction step splitting
# ---------------------------------------------------------------------------
def split_instructions(instructions_raw: str) -> list[str]:
    """Deterministically split raw recipe instructions into clean steps.

    Rule (documented for reproducibility):
      1. Split on newlines and discard empty or boilerplate lines
         (e.g. 'Watch how to make this recipe.').
      2. Strip leading explicit step numbers ('1.', 'Step 2:').
      3. If >= 2 lines remain, use those lines as the steps.
      4. If only 1 single-block paragraph remains, split on sentence boundaries
         (preserving common culinary abbreviations like 'oz.', 'tbsp.', 'approx.').
    """
    if not instructions_raw or not instructions_raw.strip():
        return []

    lines: list[str] = []
    for raw_line in instructions_raw.splitlines():
        line = _RE_MULTISPACE.sub(" ", raw_line).strip()
        if not line or _RE_BOILERPLATE_STEP.match(line):
            continue
        line = _RE_STEP_PREFIX.sub("", line).strip()
        if line:
            lines.append(line)

    if len(lines) >= 2:
        return lines
    if not lines:
        return []

    # Single-paragraph fallback: split sentences carefully
    para = lines[0]
    parts = _RE_SENTENCE_SPLIT.split(para)
    steps: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if steps and _RE_ABBREV.search(steps[-1]):
            steps[-1] = f"{steps[-1]} {part}"
        else:
            steps.append(part)
    return steps
