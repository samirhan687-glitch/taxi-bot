"""
Taxi Bot — AI Parser: location matching, text parsing, voice transcription
"""

import json
import re
import os
from pathlib import Path
from typing import Optional
from rapidfuzz import process, fuzz

# ─── Paths ────────────────────────────────────────────────────
LOCATIONS_PATH = Path(__file__).parent / "locations.json"

# ─── Uzbek direction suffixes that attach to location names ───
# "IshtxonGA" = to Ishtxon, "SamarqandDAN" = from Samarqand
DIRECTION_SUFFIXES = [
    "ga", "qa", "nga", "nga", "ga",  # to (direction)
    "dan", "dan", "daman", "danman",  # from (direction)
    "tomonga", "tomoni",              # towards
]

# ─── Passenger trigger keywords (with weights) ───────────────
# STRONG = clearly passenger; MEDIUM = likely passenger
PASSENGER_KEYWORDS_STRONG = [
    # Uzbek Latin
    "yo'lovchi", "yolovchi", "yulovchi", "йўловчи", "йуловчи",
    "пассажир", "passenger",
    "taxsi kerak", "taxi kerak", "такси керак", "такси kerak",
    "joy kerak", "joy kerakman", "ориндиқ керак",
    "olib keting", "olib ketishing",
    "qishloqqa qaytaman", "qishloqqa qaytamiz",
    "qishloqga qaytaman", "qishloqga qaytamiz",
    "uyga qaytaman", "uyga qaytamiz",
    "uyga ketaman", "uyga ketamiz",
    # "daman/danman" = passenger shorthand "from X"
    "daman", "danman", "da man",
    # Specific patterns
    "1 kishi", "2 kishi", "3 kishi",
    "bir kishi", "ikki kishi", "uch kishi",
    "yo'lovchi boraman", "yolovchi boraman",
]

PASSENGER_KEYWORDS_MEDIUM = [
    "boraman", "boramiz",
    "ketaman", "ketamiz",
    "uyga", "qishloqqa", "qishloqga", "qishloq tomonga",
]

# ─── Driver trigger keywords (with weights) ───────────────────
# STRONG = clearly driver; MEDIUM = likely driver
DRIVER_KEYWORDS_STRONG = [
    # Uzbek Latin — taxi/driver words
    "taxsi", "taxi", "такси", "такси",
    "haydovchi", "ҳайдовчи", "хайдовчи", "driver",
    # "yo'lovchi olaman" = driver offering to pick up passengers
    "yo'lovchi olaman", "yolovchi olaman", "yo'lovchi olamiz", "yolovchi olamiz",
    "йўловчи оламан", "йуловчи оламан",
    # Seat/space available
    "joy bor", "bo'sh joy", "bosh joy", "буш жой", "ориндиқ бор",
    "olib ketaman", "olib ketamiz", "olib ketish mumkin",
    "kishiga joy bor", "kshiga joy bor", "kishiga",
    # "taxsi ketaman" / "taxi ketaman" = driver departing
    "taxsi ketaman", "taxsi ketamiz", "taxi ketaman", "taxi ketamiz",
    "такси кетаман", "такси кетамиз",
    # "haydovchi ketaman" = driver departing
    "haydovchi ketaman", "haydovchi ketamiz",
    "ҳайдовчи кетаман", "хайдовчи кетаман",
]

DRIVER_KEYWORDS_MEDIUM = [
    "ertaga ketaman", "ertaga ketamiz",
    "soat",
]

# ─── "Return to village" phrases → means TO = base_location ───
RETURN_TO_VILLAGE = [
    "qishloq", "qishloqqa", "qishloqga",
    "qishloqqa qaytaman", "qishloqqa qaytamiz",
    "qishloqga qaytaman", "qishloqga qaytamiz",
    "uyga", "uyga ketaman", "uyga qaytaman", "uyga qaytamiz",
    "qishloq tomonga",
]

def strip_uz_suffix(word: str) -> str:
    """Strip Uzbek direction suffixes from a word to get the base location name.
    e.g. 'ishtixonga' → 'ishtixon' (ga), 'samarqanddan' → 'samarqand' (dan)
    
    Strategy: try all matching suffixes and return the result from the
    shortest matching suffix (produces longest base name). This prevents
    'nga' from over-stripping 'ishtixonga' → 'ishtixo' when 'ga' → 'ishtixon'
    is the correct parse.
    """
    word_lower = word.lower()
    best_stripped = None
    best_suffix_len = float('inf')
    for suffix in DIRECTION_SUFFIXES:
        if word_lower.endswith(suffix) and len(word_lower) > len(suffix) + 2:
            suffix_len = len(suffix)
            if suffix_len < best_suffix_len:
                best_suffix_len = suffix_len
                best_stripped = word_lower[:-suffix_len]
    return best_stripped if best_stripped is not None else word_lower


class LocationsManager:
    """Manage locations from JSON + fuzzy search."""

    def __init__(self, locations_path: Path = LOCATIONS_PATH):
        self.locations_path = locations_path
        self._locations: dict[str, list[str]] = {}
        self._flat_names: list[str] = []
        self._alias_to_name: dict[str, str] = {}
        self._load()

    def _load(self):
        if not self.locations_path.exists():
            self._locations = {}
            return
        with open(self.locations_path, "r", encoding="utf-8") as f:
            self._locations = json.load(f)
        self._flat_names = list(self._locations.keys())
        self._alias_to_name = {}
        for name, aliases in self._locations.items():
            self._alias_to_name[name.lower()] = name
            for alias in aliases:
                self._alias_to_name[alias.lower()] = name

    def reload(self):
        self._load()

    def get_all_names(self) -> list[str]:
        return list(self._flat_names)

    def save_pending(self, word: str) -> bool:
        """Save an unknown location word to pending list. Returns True if new."""
        pending_path = Path(__file__).parent / "pending_locations.json"
        if pending_path.exists():
            with open(pending_path, "r", encoding="utf-8") as f:
                pending = json.load(f)
        else:
            pending = []

        word_lower = word.lower()
        # Skip if already in known locations
        if word_lower in self._alias_to_name:
            return False
        # Skip if already pending
        if word_lower in [p.lower() for p in pending]:
            return False

        pending.append(word)
        with open(pending_path, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
        return True

    def get_pending(self) -> list[str]:
        """Get all pending location names."""
        pending_path = Path(__file__).parent / "pending_locations.json"
        if pending_path.exists():
            with open(pending_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    def clear_pending(self) -> None:
        """Clear all pending location names."""
        pending_path = Path(__file__).parent / "pending_locations.json"
        if pending_path.exists():
            with open(pending_path, "w", encoding="utf-8") as f:
                json.dump([], f)

    def find_best_match(self, text: str, min_score: int = 65) -> Optional[str]:
        """Find the best matching location name from text using fuzzy matching."""
        text_lower = text.lower().strip()

        # First try stripping suffixes
        stripped = strip_uz_suffix(text_lower)
        if stripped != text_lower:
            # Try the stripped version
            if stripped in self._alias_to_name:
                return self._alias_to_name[stripped]
            result = process.extractOne(
                stripped,
                self._flat_names,
                scorer=fuzz.WRatio,
                score_cutoff=min_score,
            )
            if result:
                return result[0]
            all_aliases = list(self._alias_to_name.keys())
            result = process.extractOne(
                stripped,
                all_aliases,
                scorer=fuzz.WRatio,
                score_cutoff=min_score,
            )
            if result:
                return self._alias_to_name[result[0]]

        # Direct alias lookup
        if text_lower in self._alias_to_name:
            return self._alias_to_name[text_lower]

        # Fuzzy match against all names
        result = process.extractOne(
            text_lower,
            self._flat_names,
            scorer=fuzz.WRatio,
            score_cutoff=min_score,
        )
        if result:
            return result[0]

        # Fuzzy match against all aliases
        all_aliases = list(self._alias_to_name.keys())
        result = process.extractOne(
            text_lower,
            all_aliases,
            scorer=fuzz.WRatio,
            score_cutoff=min_score,
        )
        if result:
            return self._alias_to_name[result[0]]

        return None

    def find_all_locations_in_text(self, text: str, min_score: int = 70) -> list[str]:
        """Find all location names mentioned in text, handling Uzbek suffixes."""
        found = []
        text_lower = text.lower()

        # Skip non-location keywords
        skip_words = {
            "qishloq", "qishloqqa", "qishloqga", "uy", "uyga", "salom",
            "men", "biz", "boraman", "boramiz", "ketaman", "ketamiz",
            "olib", "keting", "kerak", "yo'lovchi", "yolovchi",
            "taxsi", "taxi", "haydovchi", "driver", "joy", "bosh",
            "soat", "ertaga", "dam", "daman", "danman", "da",
            "kishi", "kishiga", "kshiga", "qaytaman", "qaytamiz",
            "olaman", "olamiz", "mumkin", "bor", "kerakman",
        }

        # Check all names and aliases (direct substring)
        for name in self._flat_names:
            aliases = self._locations.get(name, [])
            all_variants = [name.lower()] + [a.lower() for a in aliases]
            for variant in all_variants:
                if variant in text_lower and variant not in skip_words:
                    if name not in found:
                        found.append(name)
                    break

        # Split text into tokens, strip suffixes, and fuzzy match each
        words = re.findall(r"[a-zA-Zа-яА-Яўғқңҳўӯҷъ']+", text)
        for word in words:
            if len(word) < 3:
                continue
            word_lower = word.lower()
            if word_lower in skip_words:
                continue
            # Try original word
            match = self.find_best_match(word_lower, min_score)
            if match and match not in found:
                found.append(match)
                continue
            # Try with suffix stripped
            stripped = strip_uz_suffix(word_lower)
            if stripped != word_lower and len(stripped) >= 3:
                if stripped in skip_words:
                    continue
                match = self.find_best_match(stripped, min_score)
                if match and match not in found:
                    found.append(match)

        return found

    def extract_route_from_text(self, text: str, base_location: str = "Qizilqosh") -> tuple[Optional[str], Optional[str]]:
        """Extract FROM and TO locations from route patterns like 'Xdan Yga', 'X ga boraman'.
        Returns (from_loc, to_loc) as canonical location names or None.
        """
        text_lower = text.lower().strip()

        # Non-location words that should NOT be matched as route endpoints
        non_location_words = {
            "ketaman", "ketamiz", "boraman", "boramiz", "olib", "keting",
            "kerak", "yolovchi", "taxsi", "taxi", "haydovchi", "driver",
            "joy", "bosh", "soat", "ertaga", "kishi", "kishiga",
            "qaytaman", "qaytamiz", "olaman", "olamiz", "mumkin",
            "bor", "kerakman", "salom", "men", "biz",
        }

        # Split text into tokens for cleaner matching
        tokens = re.findall(r"[a-zA-Zа-яА-Яўғқңҳўӯҷъ']+", text_lower)

        # ── Pattern A: "X dan Y ga" — FROM X, TO Y (space-separated) ──
        # e.g. "samarqand dan toshkent ga"
        for i in range(len(tokens) - 3):
            if tokens[i+1] in {"dan", "daman", "danman"}:
                from_candidate = tokens[i]
                to_candidate = tokens[i+2]
                to_suffix = tokens[i+3] if i+3 < len(tokens) and tokens[i+3] in {"ga", "qa", "nga", "tomonga", "tomoni"} else None
                if to_candidate in non_location_words:
                    continue
                # Resolve FROM
                if from_candidate in {"qishloq", "uy", "shahar"}:
                    from_matched = base_location
                elif from_candidate in non_location_words:
                    continue
                else:
                    from_matched = self.find_best_match(from_candidate, min_score=55)
                # Resolve TO
                if to_suffix:
                    to_raw = to_candidate  # suffix is separate, word is clean
                else:
                    to_raw = strip_uz_suffix(to_candidate)
                if to_raw in {"qishloq", "uy", "shahar"}:
                    to_matched = base_location
                elif to_raw in non_location_words:
                    continue
                else:
                    to_matched = self.find_best_match(to_raw, min_score=55)
                if from_matched and to_matched and from_matched != to_matched:
                    return (from_matched, to_matched)

        # ── Pattern B: "XdanYga" — attached suffixes (no spaces) ──
        # e.g. "samarqanddan toshkentga" — regex on each token
        route_match = re.search(
            r"(\w+)(dan|daman|danman)\s+(\w+)(ga|qa|nga|tomonga)?",
            text_lower,
        )
        if route_match:
            from_candidate = strip_uz_suffix(route_match.group(1) + route_match.group(2))
            to_raw = strip_uz_suffix(route_match.group(3) + (route_match.group(4) or ""))
            if from_candidate not in non_location_words and to_raw not in non_location_words:
                if from_candidate in {"qishloq", "uy", "shahar"}:
                    from_matched = base_location
                else:
                    from_matched = self.find_best_match(from_candidate, min_score=55)
                if to_raw in {"qishloq", "uy", "shahar"}:
                    to_matched = base_location
                else:
                    to_matched = self.find_best_match(to_raw, min_score=55)
                if from_matched and to_matched and from_matched != to_matched:
                    return (from_matched, to_matched)

        # ── Pattern C: "X dan" / "X daman" — FROM X, TO base (space-separated) ──
        for i in range(len(tokens) - 1):
            if tokens[i+1] in {"dan", "daman", "danman"}:
                from_candidate = tokens[i]
                if from_candidate in {"qishloq", "uy", "shahar"}:
                    from_matched = base_location
                elif from_candidate in non_location_words:
                    continue
                else:
                    from_matched = self.find_best_match(from_candidate, min_score=55)
                if from_matched:
                    return (from_matched, base_location)

        # ── Pattern D: "Xdan" — FROM X, TO base (attached suffix) ──
        # Only match when suffix is at end of a complete token
        for token in tokens:
            for suffix in ["daman", "danman", "dan"]:
                if token.endswith(suffix) and len(token) > len(suffix) + 2:
                    base_word = token[:-len(suffix)]
                    if base_word in non_location_words:
                        continue
                    if base_word in {"qishloq", "uy", "shahar"}:
                        from_matched = base_location
                    else:
                        from_matched = self.find_best_match(base_word, min_score=55)
                    if from_matched:
                        return (from_matched, base_location)

        # ── Pattern E: "X ga" / "Xqa" / "Xnga" — TO X, FROM base ──
        # Space-separated suffix first
        for i in range(len(tokens) - 1):
            if tokens[i+1] in {"ga", "qa", "nga", "tomonga", "tomoni"}:
                to_candidate = tokens[i]
                if to_candidate in {"qishloq", "uy"}:
                    return (base_location, base_location)
                elif to_candidate in non_location_words:
                    continue
                else:
                    to_matched = self.find_best_match(to_candidate, min_score=55)
                if to_matched and to_matched != base_location:
                    return (base_location, to_matched)

        # Attached suffix — only at end of complete token
        for token in tokens:
            for suffix in ["tomonga", "tomoni", "nga", "ga", "qa"]:
                if token.endswith(suffix) and len(token) > len(suffix) + 2:
                    base_word = token[:-len(suffix)]
                    if base_word in {"qishloq", "uy"}:
                        to_matched = base_location
                    elif base_word in non_location_words:
                        continue
                    else:
                        to_matched = self.find_best_match(base_word, min_score=55)
                    if to_matched and to_matched != base_location:
                        return (base_location, to_matched)

        return (None, None)

    def search_locations(self, query: str, limit: int = 20) -> list[str]:
        """Search locations by query string, return up to `limit` matches."""
        query_lower = query.lower().strip()
        if not query_lower:
            return self._flat_names[:limit]

        results = process.extract(
            query_lower,
            self._flat_names,
            scorer=fuzz.partial_ratio,
            score_cutoff=50,
            limit=limit,
        )
        return [r[0] for r in results]


class AIParser:
    """Parse taxi/passenger orders from natural language text."""

    def __init__(self, locations_mgr: LocationsManager = None):
        self.locations = locations_mgr or LocationsManager()

    def determine_type(self, text: str) -> Optional[str]:
        """Determine if text is passenger or driver order using weighted keywords.

        Strategy: Check ALL keywords sorted by length (longest first).
        "taxsi kerak" (passenger) beats "taxsi" (driver)
        "yo'lovchi olaman" (driver) beats "yo'lovchi" (passenger)
        Longer compound phrases always win over their shorter substrings.
        """
        text_lower = text.lower()

        # ── 1. STRONG keywords — longest match wins ──────────────
        # Merge all strong keywords with their type, sort by length descending
        all_strong = [(kw, "driver") for kw in DRIVER_KEYWORDS_STRONG] + \
                     [(kw, "passenger") for kw in PASSENGER_KEYWORDS_STRONG]
        all_strong.sort(key=lambda x: len(x[0]), reverse=True)

        for kw, typ in all_strong:
            if kw in text_lower:
                return typ

        # ── 2. MEDIUM keywords — longest match wins ──────────────
        all_medium = [(kw, "driver") for kw in DRIVER_KEYWORDS_MEDIUM] + \
                     [(kw, "passenger") for kw in PASSENGER_KEYWORDS_MEDIUM]
        all_medium.sort(key=lambda x: len(x[0]), reverse=True)

        for kw, typ in all_medium:
            if kw in text_lower:
                return typ

        # ── 3. Heuristic: "X daman" → passenger ───────────────────
        if re.search(r"\w+\s+(daman|danman|da\s+man)", text_lower):
            return "passenger"

        # ── 4. Route pattern → passenger (if real locations found) ─
        route = self.locations.extract_route_from_text(text_lower)
        if route and route[0] and route[1]:
            all_names = self.locations.get_all_names()
            if route[0] in all_names and route[1] in all_names:
                return "passenger"

        # ── 5. Location with direction suffix → passenger ─────────
        words = re.findall(r"[a-zA-Zа-яА-Яўғқңҳўӯҷъ']+", text_lower)
        has_direction_suffix = False
        stripped_location = None
        for word in words:
            if len(word) < 3:
                continue
            stripped = strip_uz_suffix(word)
            if stripped != word.lower() and len(stripped) >= 3:
                match = self.locations.find_best_match(stripped, min_score=70)
                if match:
                    has_direction_suffix = True
                    stripped_location = match
                    break

        if has_direction_suffix and stripped_location:
            return "passenger"

        return None

    def _extract_seats(self, text: str) -> int:
        """Extract number of seats from text."""
        match = re.search(r"(\d+)\s*(kishiga|kshiga|kishi|joy)", text.lower())
        if match:
            return int(match.group(1))
        match = re.search(r"(\d+)\s*(o'rindiq|joy|seat)", text.lower())
        if match:
            return int(match.group(1))
        return 4

    def _extract_time(self, text: str) -> Optional[str]:
        """Extract time from text like '05:30 ketaman'."""
        match = re.search(r"(\d{1,2}[:\.]\d{2})", text)
        if match:
            return match.group(1)
        return None

    def parse(
        self,
        text: str,
        base_location: str = "Qizilqosh",
    ) -> Optional[dict]:
        """Parse text and return order dict or None."""
        text_lower = text.lower().strip()
        if not text_lower:
            return None

        order_type = self.determine_type(text_lower)
        if order_type is None:
            return None

        # Check for "return to village" pattern FIRST — overrides everything
        is_return = any(kw in text_lower for kw in RETURN_TO_VILLAGE)
        if is_return and "qishloq" in text_lower and not any(
            loc in text_lower for loc in ["ishtxon", "andoq", "samarqand", "vobkent"]
        ):
            # "qishloqqa" alone → need to find FROM from other locations in text
            found_other = self.locations.find_all_locations_in_text(
                text_lower, min_score=70
            )
            other_locs = [l for l in found_other if l != base_location]
            if other_locs:
                from_loc = other_locs[0]
                to_loc = base_location
            else:
                # "qishloqqa" alone without explicit FROM → assume Samarqand (nearest city)
                from_loc = "Samarqand"
                to_loc = base_location

            seats = self._extract_seats(text_lower)
            if order_type == "passenger" and seats == 4:
                seats = 1

            result = {
                "type": order_type,
                "from": from_loc,
                "to": to_loc,
                "confidence": 0.85,
                "seats": seats,
            }
            time_str = self._extract_time(text_lower)
            if time_str:
                result["time"] = time_str
            return result

        # Try route extraction first (handles "Xdan Yga" patterns)
        route_from, route_to = self.locations.extract_route_from_text(
            text_lower, base_location
        )

        from_loc = route_from
        to_loc = route_to

        # Find all locations in text
        found_locations = self.locations.find_all_locations_in_text(
            text_lower, min_score=60
        )

        # If route extraction didn't work, fall back to location-based logic
        if not from_loc or not to_loc:
            # Check for "X daman" / "X danman" pattern → FROM=X, TO=base
            daman_match = re.search(r"(\w+)\s+(daman|danman)", text_lower)
            if daman_match:
                loc_name = daman_match.group(1)
                matched = self.locations.find_best_match(loc_name, min_score=60)
                if matched:
                    from_loc = matched
                    to_loc = base_location

            # If no explicit pattern but locations found
            if not from_loc and found_locations:
                if len(found_locations) >= 2:
                    from_loc = found_locations[0]
                    to_loc = found_locations[1]
                elif len(found_locations) == 1:
                    if is_return:
                        from_loc = found_locations[0]
                        to_loc = base_location
                    else:
                        from_loc = base_location
                        to_loc = found_locations[0]

        # Final fallback
        if not from_loc:
            from_loc = base_location
        if not to_loc:
            for loc in found_locations:
                if loc != from_loc:
                    to_loc = loc
                    break
            if not to_loc and is_return:
                to_loc = base_location

        if not from_loc or not to_loc:
            return None

        # "driver" type with "joy bor" → seats from text; passenger → default 1
        seats = self._extract_seats(text_lower)
        if order_type == "passenger" and seats == 4:
            seats = 1

        time_str = self._extract_time(text_lower)

        confidence = 0.70
        if from_loc and to_loc:
            confidence += 0.10
        if order_type:
            confidence += 0.10
        if time_str:
            confidence += 0.05
        confidence = min(confidence, 0.98)

        result = {
            "type": order_type,
            "from": from_loc,
            "to": to_loc,
            "confidence": round(confidence, 2),
            "seats": seats,
        }
        if time_str:
            result["time"] = time_str

        return result


class VoiceTranscriber:
    """Transcribe voice messages using faster-whisper."""

    def __init__(self, model_size: str = "small", device: str = "cpu"):
        self.model_size = model_size
        self.device = device
        self._model = None

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type="int8",
            )
        return self._model

    def transcribe(self, audio_path: str, language: str = "uz") -> str:
        """Transcribe audio file to text."""
        model = self._load_model()
        segments, info = model.transcribe(
            audio_path,
            language=language,
            beam_size=5,
        )
        texts = []
        for segment in segments:
            texts.append(segment.text)
        return " ".join(texts).strip()

    def transcribe_auto_language(self, audio_path: str) -> str:
        """Transcribe with auto language detection (uz/ru)."""
        model = self._load_model()

        # First pass: detect language
        _, info = model.transcribe(audio_path, beam_size=1, vad_filter=True)
        lang = info.language if info.language in ("uz", "ru") else "uz"

        # Second pass: transcribe with detected language
        segments, _ = model.transcribe(
            audio_path,
            language=lang,
            beam_size=5,
            vad_filter=True,
        )
        texts = []
        for segment in segments:
            texts.append(segment.text)
        return " ".join(texts).strip()
