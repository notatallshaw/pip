from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import TYPE_CHECKING, BinaryIO, NoReturn

from pip._internal.exceptions import HashMismatch, HashMissing, InstallationError
from pip._internal.utils.misc import read_chunks

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Protocol

    class HashDigest(Protocol):
        """The digest operations required while checking bytes."""

        def update(self, data: bytes, /) -> None: ...

        def hexdigest(self) -> str: ...

    class HashChecker(Protocol):
        """Digest checks shared by data streams and cached source records."""

        def __bool__(self) -> bool: ...

        def check_against_chunks(self, chunks: Iterable[bytes]) -> dict[str, str]: ...

        def has_one_of(self, hashes: Mapping[str, str]) -> bool: ...

        def check_against_path(self, path: str) -> dict[str, str]: ...


# The recommended hash algo of the moment. Change this whenever the state of
# the art changes; it won't hurt backward compatibility.
FAVORITE_HASH = "sha256"


# Names of hashlib algorithms allowed by the --hash option and ``pip hash``
# Currently, those are the ones at least as collision-resistant as sha256.
STRONG_HASHES = ["sha256", "sha384", "sha512"]


def _hash_chunks(
    chunks: Iterable[bytes], hash_names: Iterable[str]
) -> dict[str, HashDigest]:
    """Compute requested digests in one pass over the byte chunks."""
    gots: dict[str, HashDigest] = {}
    for hash_name in hash_names:
        try:
            gots[hash_name] = hashlib.new(hash_name)
        except (ValueError, TypeError):
            raise InstallationError(f"Unknown hash name: {hash_name}")

    for chunk in chunks:
        for hash in gots.values():
            hash.update(chunk)
    return gots


class Hashes:
    """A wrapper that builds multiple hashes at once and checks them against
    known-good values

    """

    def __init__(self, hashes: dict[str, list[str]] | None = None) -> None:
        """
        :param hashes: A dict of algorithm names pointing to lists of allowed
            hex digests
        """
        allowed = {}
        if hashes is not None:
            for alg, keys in hashes.items():
                # Make sure values are always sorted (to ease equality checks)
                allowed[alg] = [k.lower() for k in sorted(keys)]
        self._allowed = allowed

    def __and__(self, other: Hashes) -> Hashes:
        if not isinstance(other, Hashes):
            return NotImplemented

        # If either of the Hashes object is entirely empty (i.e. no hash
        # specified at all), all hashes from the other object are allowed.
        if not other:
            return self
        if not self:
            return other

        # Otherwise only hashes that present in both objects are allowed.
        new = {}
        for alg, values in other._allowed.items():
            if alg not in self._allowed:
                continue
            new[alg] = [v for v in values if v in self._allowed[alg]]
        return Hashes(new)

    @property
    def digest_count(self) -> int:
        return sum(len(digests) for digests in self._allowed.values())

    def is_hash_allowed(self, hash_name: str, hex_digest: str) -> bool:
        """Return whether the given hex digest is allowed."""
        allowed = self._allowed.get(hash_name)
        return allowed is not None and hex_digest.lower() in allowed

    def check_against_chunks(self, chunks: Iterable[bytes]) -> dict[str, str]:
        """Check good hashes against ones built from iterable of chunks of
        data.

        Return the computed digests, or raise HashMismatch if none match.

        """
        gots = _hash_chunks(chunks, self._allowed)
        self._check_against_digests(gots)
        return {name: digest.hexdigest() for name, digest in gots.items()}

    def _check_against_digests(self, gots: dict[str, HashDigest]) -> None:
        for hash_name, allowed in self._allowed.items():
            if gots[hash_name].hexdigest() in allowed:
                return
        self._raise({name: gots[name] for name in self._allowed})

    def _raise(self, gots: dict[str, HashDigest]) -> NoReturn:
        raise HashMismatch(self._allowed, gots)

    def check_against_file(self, file: BinaryIO) -> dict[str, str]:
        """Verify a file's bytes and return their computed digests."""
        return self.check_against_chunks(read_chunks(file))

    def check_against_path(self, path: str) -> dict[str, str]:
        with open(path, "rb") as file:
            return self.check_against_file(file)

    def has_one_of(self, hashes: Mapping[str, str]) -> bool:
        """Return whether any of the given hashes are allowed."""
        for hash_name, hex_digest in hashes.items():
            if self.is_hash_allowed(hash_name, hex_digest):
                return True
        return False

    def __bool__(self) -> bool:
        """Return whether I know any known-good hashes."""
        return bool(self._allowed)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Hashes):
            return NotImplemented
        return self._allowed == other._allowed

    def __hash__(self) -> int:
        return hash(
            ",".join(
                sorted(
                    ":".join((alg, digest))
                    for alg, digest_list in self._allowed.items()
                    for digest in digest_list
                )
            )
        )


class HashChecks:
    """Check independent sets of allowed hashes in one pass over the data."""

    def __init__(self, checks: Iterable[Hashes]) -> None:
        self._checks = tuple(check for check in checks if check)

    def __bool__(self) -> bool:
        return bool(self._checks)

    def check_against_chunks(self, chunks: Iterable[bytes]) -> dict[str, str]:
        """Check every policy and return digests computed from the byte chunks."""
        names = dict.fromkeys(name for check in self._checks for name in check._allowed)
        gots = _hash_chunks(chunks, names)
        for check in self._checks:
            check._check_against_digests(gots)
        return {name: digest.hexdigest() for name, digest in gots.items()}

    def check_against_path(self, path: str) -> dict[str, str]:
        with open(path, "rb") as file:
            return self.check_against_chunks(read_chunks(file))

    def has_one_of(self, hashes: Mapping[str, str]) -> bool:
        return all(check.has_one_of(hashes) for check in self._checks)


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

    def _raise(self, gots: dict[str, HashDigest]) -> NoReturn:
        raise HashMissing(gots[FAVORITE_HASH].hexdigest())
