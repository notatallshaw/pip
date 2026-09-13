# More on Dependency Resolution

This article goes into more detail about pip's dependency resolution algorithm.
In certain situations, pip can take a long time to determine what to install,
and this article is intended to help readers understand what is happening
"behind the scenes" during that process.

## The dependency resolution problem

The process of finding a set of packages to install, given a set of dependencies
between them, is known to be an [NP-hard](https://en.wikipedia.org/wiki/NP-hardness)
problem. What this means in practice is roughly that the process scales
*extremely* badly as the size of the problem increases. So when you have a lot
of dependencies, working out what to install will, in the worst case, take a
very long time.

The practical implication of that is that there will always be some situations
where pip cannot determine what to install in a reasonable length of time. We
make every effort to ensure that such situations happen rarely, but eliminating
them altogether isn't even theoretically possible. We'll discuss what options
you have if you hit a problem situation like this a little later.

## Python specific issues

Dependency metadata is discovered during resolution. Pip can fetch separate metadata files when the index provides them, read metadata from downloaded distributions, or ask a source distribution's build backend to prepare it.

Fetching and preparing every available release would be costly. Pip instead reads metadata as it considers candidates and backtracks when their dependencies conflict.

## Dependency metadata

It is worth discussing precisely what metadata is needed in order to drive the
package resolution process. There are essentially three key pieces of
information:

* The project name
* The release version
* The dependencies themselves

There are other pieces of data (e.g., extras, python version restrictions, wheel
compatibility tags) which are used as well, but they do not fundamentally
alter the process, so we will ignore them here.

The most important information is the project name and version. Together with its source, they identify a candidate for installation. Name and version must be available from the
moment the candidate object is created. This is not an issue for distribution
files (sdists and wheels) as that data is available from the filename, but for
unpackaged source trees, pip needs to call the build backend to ask for that
data. This is done before resolution proper starts.

The dependency data is *not* requested in advance (as noted above, doing so
would be prohibitively costly, and for a backtracking algorithm it isn't
needed). Instead, pip requests dependency data "on demand", as the algorithm
starts to check that particular candidate.

One particular implication of the lazy fetching of dependency data is that
often, pip *does not know* things that might be obvious to a human looking at
the dependency tree as a whole. For example, if package A depends on version
1.0 of package B, it's obvious to a human that there's no point in looking at
other versions of package B. But if pip starts looking at B before it has
considered A, it doesn't have access to A's dependency data, and so has no way
of knowing that looking at other versions of B is wasted work. And worse still,
pip cannot even know that there's vital information in A's dependencies.

This latter point is a common theme with many cases where pip takes a long time
to complete a resolution - there's information pip doesn't know at the point
where it makes a "wrong" choice. Most of the heuristics added to the resolver
to guide the algorithm are designed to guess correctly in the face of that
lack of knowledge.

## The resolver and the finder

So far, we have been talking about the "resolver" as a single entity. While that
is mostly true, the process of getting package data from an index is handled
by another component of pip, the "finder". The finder is responsible for
feeding candidates to the resolver, and has a key role to play in selecting
suitable candidates.

Candidates from local source directories and {ref}`direct URL references <pypug:dependency-specifiers>` do not go through the finder. They still participate in resolution, alongside candidates obtained through the finder.

As well as determining what versions exist in the index for a given project,
the finder selects the best distribution file to use for that candidate. This
may be a wheel or a source distribution, and precisely what is selected is
controlled by wheel compatibility tags, pip's options (whether to prefer binary
or source) and metadata supplied by the index. In particular, if a file is
marked as only being for specific Python versions, the file will be ignored by
the finder (and the resolver may never even see that version).

The finder also provides candidates for a project to the resolver in order of
preference - the provider implements the rule that later versions are preferred
over older versions, for example.

## The resolver algorithm

The resolver uses nab's PubGrub algorithm through two providers. Both leave package preparation and installation policy with pip.

### Fixed-catalogue resolution

For eligible `--ignore-installed` requests, pip first resolves against fixed lists of finder candidates. Metadata is still prepared on demand; a fixed candidate list does not mean every dependency is known in advance. The provider checks dependencies before committing a candidate and reuses metadata when backtracking. It usually considers packages with fewer matching versions first. After repeated preparation of versions of a transitive package, it can restart once with command-line requirement order taking precedence over non-singleton candidate counts.

Pip rechecks the selected artifacts against native requirements, constraints, prerelease admission and finder preference before accepting the result. A URL dependency, an unsupported preparation option, a failure to resolve, or failed final admission sends the original request to the native provider. An unsuccessful fixed-catalogue solve is not proof that the original request is impossible: an unvisited package can declare a URL that supplies a missing candidate.

### Native resolution and fallback

Requests that consider installed distributions, and requests with explicit root candidates, use native resolution. Pip supplies requirements and prepared candidates through `NativeHost`, while nab tracks version and source restrictions, learns conflicts, and backtracks.

Fallback starts with a new host, provider and solver. It can reuse successfully prepared artifacts in the request's factory, but not fixed-catalogue clauses, absence conclusions or failed-preparation exclusions. Source eligibility is determined again from the original request and the dependencies considered by native resolution. A failed catalogue solve goes directly to definitive native resolution; other fallback paths can first try provisional availability and retry if validation rejects its assumptions.

Pip owns package preparation and installation policy. The factory obtains candidates from the finder, installed distributions, direct URLs, and editable projects. The host assigns source identities, translates requirements into ranges, and supplies dependency metadata when nab requests a candidate. A version from an installed distribution and the same version from a URL can have different metadata, so their source identities remain distinct.

The native provider ranks unresolved packages using their active requirements, in this order:

* Direct URL requirements.
* Exact pins using `===` or `==` without a wildcard.
* Upper version bounds using `<`, `<=`, `~=`, or `==` with a wildcard.
* Command-line requirement order.
* Other version restrictions, such as `>=` or `!=`.
* Package name.

The finder orders candidates within a package, subject to upgrade and installed-package preferences. These choices happen before all transitive metadata is available. In the diagram below, selecting between A->B and A->C may happen before pip has read the dependencies shown in grey.

![](deps.png)
