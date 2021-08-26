from rdkit.Chem import rdFMCS as MCS
import requests
from dataclasses import dataclass, asdict
import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.decomposition import PCA
import selfies as sf
import itertools
import math
import random
from . import stoned
from rdkit.Chem import MolFromSmiles as smi2mol
from rdkit.Chem.Draw import MolToImage as mol2img
import rdkit.Chem
import matplotlib.pyplot as plt
import matplotlib as mpl
from typing import *
import time
import tqdm
from ratelimit import limits, sleep_and_retry

delete_color = mpl.colors.to_rgb("#F06060")
modify_color = mpl.colors.to_rgb("#1BBC9B")


@dataclass
class Example:
    """Example of a molecule"""

    #: SMILES string for molecule
    smiles: str
    #: SELFIES for molecule, as output from :func:`selfies.encoder`
    selfies: str
    #: Tanimoto similarity relative to base
    similarity: float
    #: Output of model function
    yhat: float
    #: Index relative to other examples
    index: int
    #: PCA projected position from similarity
    position: np.ndarray = None
    #: True if base
    is_origin: bool = False
    #: Index of cluster, can be -1 for no cluster
    cluster: int = 0
    #: Label for this example
    label: str = None

    # to make it look nicer
    def __str__(self):
        return str(asdict(self))


def _fp_dist_matrix(smiles, fp_type, _pbar):
    mols = [(smi2mol(s), _pbar.update(0.5))[0] for s in smiles]
    # Sorry about the one-line. Just sneaky insertion of progressbar update
    fp = [(stoned.get_fingerprint(m, fp_type), _pbar.update(0.5))[0]
          for m in mols]
    # 1 - Ts because we want distance
    dist = list(
        1 - stoned.TanimotoSimilarity(x, y) for x, y in itertools.product(fp, repeat=2)
    )
    return np.array(dist).reshape(len(mols), len(mols))


def get_basic_alphabet() -> Set[str]:
    """Returns set of interpretable SELFIES tokens

    Generated by removing P and most ionization states from :func:`selfies.get_semantic_robust_alphabet`
    """
    a = sf.get_semantic_robust_alphabet()
    # remove cations/anions except oxygen anion
    to_remove = []
    for ai in a:
        if "+1" in ai:
            to_remove.append(ai)
        elif "-1" in ai:
            to_remove.append(ai)
    # remove [P],[#P],[=P]
    to_remove.extend(["[P]", "[#P]", "[=P]"])

    a -= set(to_remove)
    a.add("[O-1expl]")
    return a


def run_stoned(
    s: str,
    fp_type: str = "ECFP4",
    num_samples: int = 2000,
    max_mutations: int = 2,
    min_mutations: int = 1,
    alphabet: Union[List[str], Set[str]] = None,
    _pbar: Any = None
) -> Tuple[List[str], List[str]]:
    """Run ths STONED SELFIES algorithm. Typically not used, call :func:`sample_space` instead.

    :param s: SMILES string to start from
    :param fp_type: Fingerprint type
    :param num_samples: Number of molecules to generate per mutation
    :param max_mutations: Maximum number of mutations
    :param min_mutations: Minimum number of mutations
    :param alphabet: Alphabet to use for mutations, typically from :func:`get_basic_alphabet()`
    :return: SMILES and SELFIES generated
    """
    if alphabet is None:
        alphabet = list(sf.get_semantic_robust_alphabet())
    if type(alphabet) == set:
        alphabet = list(alphabet)
    num_mutation_ls = list(range(min_mutations, max_mutations + 1))

    mol = smi2mol(s)
    if mol == None:
        raise Exception("Invalid starting structure encountered")

    randomized_smile_orderings = [
        stoned.randomize_smiles(mol) for _ in range(num_samples)
    ]

    # Convert all the molecules to SELFIES
    selfies_ls = [sf.encoder(x) for x in randomized_smile_orderings]

    all_smiles_collect = []
    all_selfies_collect = []
    for num_mutations in num_mutation_ls:
        # Mutate the SELFIES:
        if _pbar:
            _pbar.set_description(f'🥌STONED🥌 Mutations: {num_mutations}')
        selfies_mut = stoned.get_mutated_SELFIES(
            selfies_ls.copy(), num_mutations=num_mutations, alphabet=alphabet
        )
        # Convert back to SMILES:
        smiles_back = [sf.decoder(x) for x in selfies_mut]
        all_smiles_collect = all_smiles_collect + smiles_back
        all_selfies_collect = all_selfies_collect + selfies_mut
        if _pbar:
            _pbar.update(len(smiles_back))

    # Work on:  all_smiles_collect
    if _pbar:
        _pbar.set_description(f'🥌STONED🥌 Done')
    canon_smi_ls = []
    for item in all_smiles_collect:
        mol, smi_canon, did_convert = stoned.sanitize_smiles(item)
        if mol == None or smi_canon == "" or did_convert == False:
            raise Exception("Invalid smiles string found")
        canon_smi_ls.append(smi_canon)

    # remove redundant/non-unique/duplicates
    # in a way to keep the selfies
    canon_smi_ls = list(set(canon_smi_ls))

    canon_smi_ls_scores = stoned.get_fp_scores(
        canon_smi_ls, target_smi=s, fp_type=fp_type
    )
    # NOTE Do not think of returning selfies. They have duplicates
    return canon_smi_ls, canon_smi_ls_scores

FIFTEEN_MINUTES=900

@sleep_and_retry
@limits(calls=50, period=FIFTEEN_MINUTES)
def run_chemed(
    origin_smiles: str,
    num_samples: int,
    similarity: float = 0.1,
    fp_type: str = 'ECFP4',
    _pbar: Any = None,
) -> Tuple[List[str], List[float]]:
    """
    This method is similar to STONED but works by quering PubChem
    :param origin_smiles: Base SMILES
    :param num_samples: Minimum number of returned molecules. May return less due to network timeout or exhausting tree
    :param similarity: Tanimoto similarity to use in query (float between 0 to 1)
    :return: SMILES
    """
    if _pbar:
        _pbar.set_description('⚡CHEMED⚡ is Experimental ☠️')
    else:
        print('⚡CHEMED⚡ is Experimental ☠️')
    url = f'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/fastsimilarity_2d/smiles/{origin_smiles}/property/CanonicalSMILES/JSON'
    try:
        reply = requests.get(url, params={
                         'Threshold': int(similarity), 'MaxRecords':num_samples},
                             headers={'accept': 'text/json'},
                            timeout=10)
    except requests.exceptions.Timeout:
        print('Pubchem seems to be down right now ️☠️☠️')
        return [], []
    try:
        data = reply.json()
    except:
        return [], []
    smiles = [d['CanonicalSMILES'] for d in data['PropertyTable']['Properties']]
    smiles = set(smiles)

    if _pbar: _pbar.set_description(f'Received {len(smiles)} similar molecules')

    mol0 = smi2mol(origin_smiles)
    mols = [smi2mol(s) for s in smiles]
    fp0 = stoned.get_fingerprint(mol0, fp_type)
    scores = []
    # drop Nones
    smiles = [s for s,m in zip(smiles, mols) if m is not None]
    for m in mols:
        if m is None:
            continue
        fp  = stoned.get_fingerprint(m, fp_type)
        scores.append(stoned.TanimotoSimilarity(fp0, fp))
        if _pbar: _pbar.update()
    return smiles, scores



def sample_space(
    origin_smiles: str,
    f: Union[
        Callable[[str, str], float], Callable[[
            List[str], List[str]], List[float]]
    ],
    batched: bool = True,
    preset: str = "medium",
    method_kwargs: Dict = None,
    num_samples: int = None,
    stoned_kwargs: Dict = None
) -> List[Example]:
    """Sample chemical space around given SMILES

    This will evaluate the given function and run the :func:`run_stoned` function over chemical space around molecule.

    :param origin_smiles: starting SMILES
    :param f: A function which takes in SMILES and SELFIES and returns predicted value. Assumed to work with lists of SMILES/SELFIES unless `batched = False`
    :param batched: If `f` is batched
    :param preset: Can be wide, medium, or narrow. Determines how far across chemical space is sampled. Try `"chemed"` experimental preset to only sample commerically available compounds.
    :param method_kwargs: More control over STONED or CHEMED can be set here. See :func:`run_stoned` and :func:`run_chemed`
    :param num_samples: Number of desired samples. Can be set in `method_kwargs` (overrides) or here. `None` means default from preset.
    :param stoned_kwargs: Backwards compatible alias for `methods_kwargs`
    :return: List of generated :obj:`Example`
    """
    batched_f = f
    if not batched:

        def batched_f(sm, se):
            return np.array([f(smi, sei) for smi, sei in zip(sm, se)])

    smi_yhat = batched_f([origin_smiles], [sf.encoder(origin_smiles)])
    try:
        iter(smi_yhat)
    except TypeError:
        raise ValueError("Your model function does not appear to be batched")
    smi_yhat = np.squeeze(smi_yhat[0])

    if stoned_kwargs is not None:
        method_kwargs = stoned_kwargs

    if method_kwargs is None:
        method_kwargs = {}
        if preset == "medium":
            method_kwargs["num_samples"] = 1500 if num_samples is None else num_samples
            method_kwargs["max_mutations"] = 2
            method_kwargs["alphabet"] = get_basic_alphabet()
        elif preset == "narrow":
            method_kwargs["num_samples"] = 3000 if num_samples is None else num_samples
            method_kwargs["max_mutations"] = 1
            method_kwargs["alphabet"] = get_basic_alphabet()
        elif preset == "wide":
            method_kwargs["num_samples"] = 600 if num_samples is None else num_samples
            method_kwargs["max_mutations"] = 5
            method_kwargs["alphabet"] = sf.get_semantic_robust_alphabet()
        elif preset == "chemed":
            method_kwargs["num_samples"] = 150 if num_samples is None else num_samples
        else:
            raise ValueError(f'Unknown preset "{preset}"')
    try:
        num_samples = method_kwargs["num_samples"]
    except KeyError as e:
        if num_samples is None:
            num_samples = 150
        method_kwargs["num_samples"] = num_samples

    pbar = tqdm.tqdm(total=num_samples)

    # STONED
    if preset.startswith("chem"):
        smiles, scores = run_chemed(origin_smiles, _pbar=pbar, **method_kwargs)
    else:
        smiles, scores = run_stoned(origin_smiles, _pbar=pbar, **method_kwargs)
    selfies = [sf.encoder(s) for s in smiles]

    pbar.set_description('😀Calling your model function😀')
    fxn_values = batched_f(smiles, selfies)

    # pack them into data structure with filtering out identical
    # and nan
    exps = [
        Example(
            origin_smiles,
            sf.encoder(origin_smiles),
            1.0,
            smi_yhat,
            index=0,
            is_origin=True,
        )
    ] + [
        Example(sm, se, s, np.squeeze(y), index=0)
        for i, (sm, se, s, y) in enumerate(zip(smiles, selfies, scores, fxn_values))
        if s < 1.0 and np.isfinite(np.squeeze(y))
    ]
    for i, e in enumerate(exps):
        e.index = i

    pbar.reset(len(exps))
    pbar.set_description('🔭Projecting...🔭')

    # compute distance matrix
    full_dmat = _fp_dist_matrix(
        [e.smiles for e in exps],
        method_kwargs["fp_type"] if ("fp_type" in method_kwargs) else "ECFP4",
        _pbar=pbar
    )

    pbar.set_description('🥰Finishing up🥰')

    # compute PCA
    pca = PCA(n_components=2)
    proj_dmat = pca.fit_transform(full_dmat)
    for e in exps:
        e.position = proj_dmat[e.index, :]

    # do clustering everwhere (maybe do counter/same separately?)
    # clustering = AgglomerativeClustering(
    #    n_clusters=max_k, affinity='precomputed', linkage='complete').fit(full_dmat)
    # Just do it on projected so it looks prettier.
    clustering = DBSCAN(eps=0.15, min_samples=5).fit(proj_dmat)

    for i, e in enumerate(exps):
        e.cluster = clustering.labels_[i]

    pbar.set_description('🤘Done🤘')
    pbar.close()
    return exps


def _select_examples(cond, examples, nmols):
    result = []

    # similarit filtered by if cluster/counter
    def cluster_score(e, i):
        return (e.cluster == i) * cond(e) * e.similarity

    clusters = set([e.cluster for e in examples])
    for i in clusters:
        close_counter = max(examples, key=lambda e, i=i: cluster_score(e, i))
        # check if actually is (since call could have been off)
        if cluster_score(close_counter, i):
            result.append(close_counter)

    # trim, in case we had too many cluster
    result = sorted(result, key=lambda v: v.similarity *
                    cond(v), reverse=True)[:nmols]

    # fill in remaining
    ncount = sum([cond(e) for e in result])
    fill = max(0, nmols - ncount)
    result.extend(
        sorted(examples, key=lambda v: v.similarity *
               cond(v), reverse=True)[:fill]
    )

    return list(filter(cond, result))


def cf_explain(examples: List[Example], nmols: int = 3) -> List[Example]:
    """From given :obj:`Examples`, find best counterfactuals using :ref:`readme_link:counterfactual generation`

    :param examples: Output from :func:`sample_space`
    :param nmols: Desired number of molecules
    """

    def is_counter(e):
        return e.yhat != examples[0].yhat

    result = _select_examples(is_counter, examples[1:], nmols)
    for i, r in enumerate(result):
        r.label = f"Counterfactual {i+1}"

    return examples[:1] + result


def rcf_explain(
    examples: List[Example],
    delta: Union[float, Tuple[float, float]] = (-1, 1),
    nmols: int = 4,
) -> List[Example]:
    """From given :obj:`Examples`, find best counterfactuals using :ref:`readme_link:counterfactual generation`

    This version works with regression, so that a counterfactual is if the given example is higher or
    lower than base.

    :param examples: Output from :func:`sample_space`
    :param delta: float or tuple of hi/lo indicating margin for what is counterfactual
    :param nmols: Desired number of molecules
    """
    if type(delta) is float:
        delta = (-delta, delta)

    def is_high(e):
        return e.yhat + delta[0] >= examples[0].yhat

    def is_low(e):
        return e.yhat + delta[1] <= examples[0].yhat

    hresult = (
        [] if delta[0] is None else _select_examples(
            is_high, examples[1:], nmols // 2)
    )
    for i, h in enumerate(hresult):
        h.label = f"Increase ({i+1})"
    lresult = (
        [] if delta[1] is None else _select_examples(
            is_low, examples[1:], nmols // 2)
    )
    for i, l in enumerate(lresult):
        l.label = f"Decrease ({i+1})"
    return examples[:1] + lresult + hresult


def _mol_images(exps, mol_size, fontsize):
    if len(exps) == 0:
        return []
    # get aligned images
    ms = [smi2mol(e.smiles) for e in exps]
    dos = rdkit.Chem.Draw.MolDrawOptions()
    dos.useBWAtomPalette()
    dos.minFontSize = fontsize
    rdkit.Chem.AllChem.Compute2DCoords(ms[0])
    imgs = []
    for m in ms[1:]:
        rdkit.Chem.AllChem.GenerateDepictionMatching2DStructure(
            m, ms[0], acceptFailure=True
        )
        aidx, bidx = moldiff(ms[0], m)
        # if it is too large, we ignore it
        # if len(aidx) > 8:
        #    aidx = []
        #    bidx = []
        imgs.append(
            mol2img(
                m,
                size=mol_size,
                options=dos,
                highlightAtoms=aidx,
                highlightBonds=bidx,
                highlightColor=modify_color if len(bidx) > 0 else delete_color,
            )
        )
    rdkit.Chem.AllChem.GenerateDepictionMatching2DStructure(
        ms[0], ms[1], acceptFailure=True
    )
    imgs.insert(0, mol2img(ms[0], size=mol_size, options=dos))
    return imgs


def plot_space(
    examples: List[Example],
    exps: List[Example],
    figure_kwargs: Dict = None,
    mol_size: Tuple[int, int] = (200, 200),
    highlight_clusters: bool = False,
    mol_fontsize: int = 8,
    offset: int = 0,
    ax: Any = None
):
    """Plot chemical space around example and annotate given examples.

    :param examples: Large list of :obj:Example which make-up points
    :param exps: Small list of :obj:Example which will be annotated
    :param figure_kwargs: kwargs to pass to :func:`plt.figure<matplotlib.pyplot.figure>`
    :param mol_size: size of rdkit molecule rendering, in pixles
    :param highlight_clusters: if `True`, cluster indices are rendered instead of :obj:Example.yhat
    :param mol_fontsize: minimum font size passed to rdkit
    :param offset: offset annotations to allow colorbar or other elements to fit into plot.
    :param fig: axis onto which to plot
    """
    imgs = _mol_images(exps, mol_size, mol_fontsize)
    if figure_kwargs is None:
        figure_kwargs = {"figsize": (12, 8)}
    base_color = "gray"
    if ax is None:
        ax = plt.figure(**figure_kwargs).gca()
    if highlight_clusters:
        colors = [e.cluster for e in examples]

        def normalizer(x):
            return x

        cmap = "Accent"
    else:
        colors = [e.yhat for e in examples]
        normalizer = plt.Normalize(min(colors), max(colors))
        cmap = "viridis"
    ax.scatter(
        [e.position[0] for e in examples],
        [e.position[1] for e in examples],
        c=normalizer(colors),
        cmap=cmap,
        alpha=0.5,
        edgecolors="none",
    )
    ax.scatter(
        [e.position[0] for e in exps],
        [e.position[1] for e in exps],
        c=normalizer(
            [e.cluster if highlight_clusters else e.yhat for e in exps]),
        cmap=cmap,
        edgecolors="black",
    )

    x = [e.position[0] for e in exps]
    y = [e.position[1] for e in exps]
    titles = []
    colors = []
    for e in exps:
        if not e.is_origin:
            titles.append(f"Similarity = {e.similarity:.2f}\n{e.label}")
            colors.append(base_color)
        else:
            titles.append("Base")
            colors.append(base_color)
    _image_scatter(x, y, imgs, titles, colors, plt.gca(), offset=offset)
    ax.axis("off")
    ax.set_aspect("auto")


def _nearest_spiral_layout(x, y, offset):
    # make spiral
    angles = np.linspace(-np.pi, np.pi, len(x) + 1 + offset)[offset:]
    coords = np.stack((np.cos(angles), np.sin(angles)), -1)
    order = np.argsort(np.arctan2(y, x))
    return coords[order]


def _image_scatter(x, y, imgs, subtitles, colors, ax, offset):
    from matplotlib.offsetbox import OffsetImage, AnnotationBbox, TextArea, VPacker

    box_coords = _nearest_spiral_layout(x, y, offset)
    bbs = []
    for i, (x0, y0, im, t, c) in enumerate(zip(x, y, imgs, subtitles, colors)):
        # add transparency
        im = trim(im)
        img_data = np.asarray(im)
        img_box = OffsetImage(img_data)
        title_box = TextArea(t)
        packed = VPacker(children=[img_box, title_box],
                         pad=0, sep=4, align="center")
        bb = AnnotationBbox(
            packed,
            (x0, y0),
            frameon=True,
            xybox=box_coords[i] + 0.5,
            arrowprops=dict(arrowstyle="->", edgecolor="black"),
            pad=0.3,
            boxcoords="axes fraction",
            bboxprops=dict(edgecolor=c),
        )
        ax.add_artist(bb)
        bbs.append(bb)
    return bbs


def plot_cf(
    exps: List[Example],
    fig: Any = None,
    figure_kwargs: Dict = None,
    mol_size: Tuple[int, int] = (200, 200),
    mol_fontsize: int = 10,
    nrows: int = None,
    ncols: int = None,
):
    """Draw the given set of Examples in a grid

    :param exps: Small list of :obj:Example which will be drawn
    :param fig: Figure to plot onto
    :param figure_kwargs: kwargs to pass to :func:`plt.figure<matplotlib.pyplot.figure>`
    :param mol_size: size of rdkit molecule rendering, in pixles
    :param mol_fontsize: minimum font size passed to rdkit
    :param nrows: number of rows to draw in grid
    :param ncols: number of columns to draw in grid
    """
    imgs = _mol_images(exps, mol_size, mol_fontsize)
    if nrows is not None:
        R = nrows
    else:
        R = math.ceil(math.sqrt(len(imgs)))
    if ncols is not None:
        C = ncols
    else:
        C = math.ceil(len(imgs) / R)
    if fig is None:
        if figure_kwargs is None:
            figure_kwargs = {'figsize': (12,8)}
        fig, axs = plt.subplots(R, C, **figure_kwargs)
    else:
        axs = fig.subplots(R, C)
    axs = axs.flatten()
    for i, (img, e) in enumerate(zip(imgs, exps)):
        title = "Base" if e.is_origin else f"Similarity = {e.similarity:.2f}\n{e.label}"
        title += f"\nf(x) = {e.yhat:.3f}"
        axs[i].set_title(title)
        axs[i].imshow(np.asarray(img))
        axs[i].axis("off")
    for j in range(i, C * R):
        axs[j].axis("off")
        axs[j].set_facecolor("white")
    plt.tight_layout()


def moldiff(template, query) -> Tuple[List[int], List[int]]:
    """Compare the two rdkit molecules.

    :param template: template molecule
    :param query: query molecule
    :return: list of modified atoms in query, list of modified bonds in query
    """
    r = MCS.FindMCS([template, query])
    substructure = rdkit.Chem.MolFromSmarts(r.smartsString)
    raw_match = query.GetSubstructMatches(substructure)
    template_match = template.GetSubstructMatches(substructure)
    # flatten it
    match = list(raw_match[0])
    template_match = list(template_match[0])

    # need to invert match to get diffs
    inv_match = [i for i in range(query.GetNumAtoms()) if i not in match]

    # get bonds
    bond_match = []
    for b in query.GetBonds():
        if b.GetBeginAtomIdx() in inv_match or b.GetEndAtomIdx() in inv_match:
            bond_match.append(b.GetIdx())

    # now get bonding changes from deletion

    def neigh_hash(a):
        return "".join(sorted([n.GetSymbol() for n in a.GetNeighbors()]))

    for ti, qi in zip(template_match, match):
        if neigh_hash(template.GetAtomWithIdx(ti)) != neigh_hash(
            query.GetAtomWithIdx(qi)
        ):
            inv_match.append(qi)

    return inv_match, bond_match


def trim(im):
    """Implementation of whitespace trim

    credit: https://stackoverflow.com/a/10616717

    :param im: PIL image
    :return: PIL image
    """
    from PIL import Image, ImageChops

    # https://stackoverflow.com/a/10616717
    bg = Image.new(im.mode, im.size, im.getpixel((0, 0)))
    diff = ImageChops.difference(im, bg)
    diff = ImageChops.add(diff, diff, 2.0, -100)
    bbox = diff.getbbox()
    if bbox:
        return im.crop(bbox)
