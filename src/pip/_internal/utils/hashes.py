from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import TYPE_CHECKING, BinaryIO, NoReturn

from pip._internal.exceptions import HashMismatch, HashMissing, InstallationError
from pip._internal.utils.misc import read_chunks

if TYPE_CHECKING:
    from collections.abc import Mapping
    from hashlib import _Hash

# The recommended hash algo of the moment. Change this whenever the state of
# the art changes; it won't hurt backward compatibility.
FAVORITE_HASH = "sha256"


# Names of hashlib algorithms allowed by the --hash option and ``pip hash``
# Currently, those are the ones at least as collision-resistant as sha256.
STRONG_HASHES = ["sha256", "sha384", "sha512"]


class Hashes:
    """A wrapper that builds multiple hashes at once and checks them against
    known-good values

    """

    def __init__(
        self,
        hashes: dict[str, list[str]] | None = None,
        *,
        required: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        """
        :param hashes: A dict of algorithm names pointing to lists of allowed
            hex digests
        :param required: Digests that must each match independently.
        """
        allowed = {}
        if hashes is not None:
            for alg, keys in hashes.items():
                # Make sure values are always sorted (to ease equality checks)
                allowed[alg] = [k.lower() for k in sorted(keys)]
        self._allowed = allowed
        self._required = required

    def __and__(self, other: Hashes) -> Hashes:
        if not isinstance(other, Hashes):
            return NotImplemented

        # If either of the Hashes object is entirely empty (i.e. no hash
        # specified at all), all hashes from the other object are allowed.
        if not other:
            return self
        if not self:
            return other

        required = self._required | other._required
        if not self._allowed or not other._allowed:
            return Hashes(self._allowed or other._allowed, required=required)

        # Otherwise only hashes that present in both objects are allowed.
        new = {}
        for alg, values in other._allowed.items():
            if alg not in self._allowed:
                continue
            new[alg] = [v for v in values if v in self._allowed[alg]]
        return Hashes(new, required=required)

    @property
    def digest_count(self) -> int:
        return sum(len(digests) for digests in self._allowed.values()) + len(
            self._required
        )

    def is_hash_allowed(self, hash_name: str, hex_digest: str) -> bool:
        """Return whether the given hex digest is allowed."""
        required_match = all(
            algorithm == hash_name and digest == hex_digest
            for algorithm, digest in self._required
        )
        return required_match and (
            (bool(self._required) and not self._allowed)
            or hex_digest in self._allowed.get(hash_name, [])
        )

    def check_against_chunks(self, chunks: Iterable[bytes]) -> None:
        """Check good hashes against ones built from iterable of chunks of
        data.

        Raise HashMismatch if none match.

        """
        gots = {}
        for hash_name in self._allowed.keys() | {name for name, _ in self._required}:
            try:
                gots[hash_name] = hashlib.new(hash_name)
            except (ValueError, TypeError):
                raise InstallationError(f"Unknown hash name: {hash_name}")

        for chunk in chunks:
            for hash in gots.values():
                hash.update(chunk)

        for hash_name, expected in self._required:
            if gots[hash_name].hexdigest() != expected:
                raise HashMismatch(
                    {hash_name: [expected]}, {hash_name: gots[hash_name]}
                )
        if self._required and not self._allowed:
            return
        for hash_name, expected_digests in self._allowed.items():
            if gots[hash_name].hexdigest() in expected_digests:
                return
        self._raise(gots)

    def _raise(self, gots: dict[str, _Hash]) -> NoReturn:
        raise HashMismatch(self._allowed, gots)

    def check_against_file(self, file: BinaryIO) -> None:
        """Check good hashes against a file-like object

        Raise HashMismatch if none match.

        """
        return self.check_against_chunks(read_chunks(file))

    def check_against_path(self, path: str) -> None:
        with open(path, "rb") as file:
            return self.check_against_file(file)

    def has_one_of(self, hashes: Mapping[str, str]) -> bool:
        """Return whether any of the given hashes are allowed."""
        if not all(hashes.get(name) == digest for name, digest in self._required):
            return False
        if self._required and not self._allowed:
            return True
        for hash_name, hex_digest in hashes.items():
            if self.is_hash_allowed(hash_name, hex_digest):
                return True
        return False

    def __bool__(self) -> bool:
        """Return whether I know any known-good hashes."""
        return bool(self._allowed or self._required)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Hashes):
            return NotImplemented
        return self._allowed == other._allowed and self._required == other._required

    def __hash__(self) -> int:
        return hash(
            (
                ",".join(
                    sorted(
                        ":".join((alg, digest))
                        for alg, digest_list in self._allowed.items()
                        for digest in digest_list
                    )
                ),
                self._required,
            )
        )


class MissingHashes(Hashes):
    """A workalike for Hashes used when we're missing a hash for a requirement

    It computes the actual hash of the requirement and raises a HashMissing
    exception showing it to the user.

    """

    def __init__(self) -> None:
        """Don't offer the ``hashes`` kwarg."""
        # Pass our favorite hash in to generate a "gotten hash". With the
        # empty list, it will never match, so an error will always raise.
        super().__init__(hashes={FAVORITE_HASH: []})

    def _raise(self, gots: dict[str, _Hash]) -> NoReturn:
        raise HashMissing(gots[FAVORITE_HASH].hexdigest())
