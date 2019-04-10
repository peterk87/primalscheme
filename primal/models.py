import logging
import os
import sys
import pickle
import primer3
import re
import settings
from copy import copy

from Bio import pairwise2, Seq, SeqIO
from Bio.Graphics import GenomeDiagram
from Bio.SeqFeature import FeatureLocation, SeqFeature
from reportlab.lib import colors

from exceptions import NoSuitableError

sys.path.append('Porechop/porechop')
from cpp_function_wrappers import adapter_alignment

logger = logging.getLogger('Primal Log')


class Primer(object):
    """A simple primer."""

    def __init__(self, direction, name, seq):
        self.direction = direction
        self.name = name
        self.seq = seq
        self.tm = primer3.calcTm(self.seq, dv_conc=1.5, dntp_conc=0.6)
        self.gc = 100.0 * (seq.count('G') + seq.count('C')) / len(seq)

    @property
    def length(self):
        return len(self.seq)


class CandidatePrimer(Primer):
    """A candidate primer for a region."""

    def __init__(self, direction, name, seq, start, references):
        super(CandidatePrimer, self).__init__(direction, name, seq)
        self.start = start
        self.percent_identity = 0
        self.alignments = []
        self.references = references

    def align(self):
        percent_identity = 0
        for ref in self.references:
            alignment = CAlignment(self, ref)
            self.alignments.append(alignment)
            percent_identity += alignment.percent_identity

        # Calculate average percent identity
        self.percent_identity = percent_identity / len(self.alignments)
        return(self)

    @property
    def end(self):
        if self.direction == 'LEFT':
            return self.start + self.length
        else:
            return self.start - self.length


class CandidatePrimerPair(object):
    """A pair of candidate primers for a region."""

    def __init__(self, left, right):
        self.left = left
        self.right = right
        # Calculate mean percent identity
        self.mean_percent_identity = (left.percent_identity + right.percent_identity) / 2

    @property
    def product_length(self):
        return self.right.start - self.left.start + 1

class Region(object):
    """A region that forms part of a scheme."""
    def __init__(self, region_num, chunk_start, candidate_pairs, references, prefix, max_alts=0):
        self.region_num = region_num
        self.pool = '2' if self.region_num % 2 == 0 else '1'
        self.candidate_pairs = candidate_pairs
        self.alternates = []

        # Sort by highest scoring pair with the rightmost position
        self.candidate_pairs.sort(key=lambda x: (x.mean_percent_identity, x.right.end), reverse=True)

        # Align candidate pairs
        for pair in self.candidate_pairs:
            pair.left.align()
            pair.right.align()

        # Get a list of alts based on the alignments
        left_alts = [str(each.aln_ref) for each in self.candidate_pairs[0].left.alignments if each.aln_ref != self.candidate_pairs[0].left.seq]
        right_alts = [str(each.aln_ref) for each in self.candidate_pairs[0].right.alignments if each.aln_ref != self.candidate_pairs[0].right.seq]

        # Get the counts for the alts to prioritise
        left_alts_counts = [(alt, left_alts.count(alt)) for alt in set(left_alts)]
        left_alts_counts.sort(key=lambda x: x[1], reverse=True)

        # Make tuples of unique primers and frequency and sort
        right_alts_counts = [(alt, right_alts.count(alt)) for alt in set(right_alts)]
        right_alts_counts.sort(key=lambda x: x[1], reverse=True)

        # For up to max_alts and if it occurs more than once generate a CandidatePrimer and add it to alternates list in Region
        for n, left_alt in enumerate(left_alts_counts):
            # max_alts is 1-indexed
            if n <= max_alts - 1 and left_alt[1] > 1:
                logger.debug('Found an alternate primer {} which covers {} reference sequences'.format(n, left_alt[1]))
                left = CandidatePrimer('LEFT', self.candidate_pairs[0].left.name + '_alt%i' %(n+1), left_alt[0], self.candidate_pairs[0].left.start, references).align()
                self.alternates.append(left)

        for n, right_alt in enumerate(right_alts_counts):
            if n <= max_alts - 1 and right_alt[1] > 1:
                logger.debug('Found an alternate primer {} which covers {} reference sequences'.format(n, right_alt[1]))
                right = CandidatePrimer('RIGHT', self.candidate_pairs[0].right.name + '_alt%i' %(n+1), right_alt[0], self.candidate_pairs[0].right.start, references).align()
                self.alternates.append(right)

    @property
    def top_pair(self):
        return self.candidate_pairs[0]

    @property
    def unique_candidates(self):
        unique_left = len(set(pair.left.seq for pair in self.candidate_pairs))
        unique_right = len(set(pair.right.seq for pair in self.candidate_pairs))
        return [unique_left, unique_right]

class CAlignment(object):
    """An seqan alignment of a primer against a reference."""
    def __init__(self, primer, ref):
        if primer.direction == 'LEFT':
            alignment_result = adapter_alignment(str(ref.seq), str(primer.seq), [2, -1, -2, -1])
        elif primer.direction == 'RIGHT':
            alignment_result = adapter_alignment(str(ref.seq.reverse_complement()), str(primer.seq), [2, -1, -2, -1])
        result_parts = alignment_result.split(',')
        ref_start = int(result_parts[0])

        # If the read start is -1, that indicates that the alignment failed completely.
        if ref_start == -1:
            self.percent_identity = 0.0
            self.formatted_alignment = 'None'
        else:
            ref_end = int(result_parts[1]) + 1
            #aligned_region_percent_identity = float(result_parts[5])
            full_primer_percent_identity = float(result_parts[6])

            if primer.direction == 'LEFT':
                self.start = ref_start
                self.end = ref_end
                self.length = self.end - self.start
            else:
                self.start = len(ref) - ref_start
                self.end = len(ref) - (int(result_parts[1]) + 1)
                self.length = self.start - self.end

            # Percentage identity for glocal alignment
            self.percent_identity = full_primer_percent_identity

            # Get alignment strings
            self.aln_query = result_parts[8][ref_start:ref_end]
            self.aln_ref = result_parts[7][ref_start:ref_end]
            self.aln_ref_comp = Seq.Seq(str(self.aln_ref)).complement()
            self.ref_id = ref.id
            self.mm_3prime = False

            # Make cigar
            self.cigar = ''
            for a, b in zip(self.aln_query, self.aln_ref):
                if a == '-' or b == '-':
                    self.cigar += ' '
                    continue
                if a != b:
                    self.cigar += '*'
                    continue
                else:
                    self.cigar += '|'

            # Format alignment
            short_primer = primer.name[:30] if len(primer.name) > 30 else primer.name
            short_ref = ref.id[:30] if len(ref.id) > 30 else ref.id
            self.formatted_alignment = "\n{: <30}5\'-{}-3\'\n{: <33}{}\n{: <30}3\'-{}-5\'".format(short_primer, self.aln_query, '', self.cigar, short_ref, self.aln_ref_comp)

            # Check 3' mismatches
            if set([self.aln_query[-1], self.aln_ref_comp[-1]]) in settings.MISMATCHES:
                self.mm_3prime = True
                self.percent_identity = 0


class MultiplexScheme(object):
    """A complete multiplex primer scheme."""

    def __init__(self, references, amplicon_length, min_overlap, max_gap, max_alts, max_candidates,
                 step_size, max_variation, prefix='PRIMAL_SCHEME'):
        self.references = references
        self.amplicon_length = amplicon_length
        self.min_overlap = min_overlap
        self.max_gap = max_gap
        self.max_alts = max_alts
        self.max_candidates = max_candidates
        self.step_size = step_size
        self.max_variation = max_variation
        self.prefix = prefix
        self.regions = []

        self.run()

    @property
    def primary_reference(self):
        return self.references[0]

    def run(self):
        regions = []
        region_num = 0
        is_last_region = False

        while True:
            region_num += 1
            # Get the previous region in each pool
            prev_pair = regions[-1].candidate_pairs[0] if len(regions) >= 1 else None
            prev_pair_same_pool = regions[-2].candidate_pairs[0] if len(regions) > 2 else None

            # If there are two regions or more
            if prev_pair_same_pool:
                # Gap opened between -1 and -2 regions
                if prev_pair.left.start > prev_pair_same_pool.right.start:
                    # If there was a gap left primer cannot overlap with -1 region
                    left_primer_left_limit = prev_pair.left.end + 1
                else:
                    # Left primer cannot overlap -2 region
                    left_primer_left_limit = prev_pair_same_pool.right.start + 1
            # If there is more than one region
            elif prev_pair:
                # Left primer cannot overlap with -1 region or you don't move
                left_primer_left_limit = prev_pair.left.end + 1
            else:
                # Region one only limit is 0
                left_primer_left_limit = 0

            # Right start limit maintains the minimum_overlap
            left_primer_right_limit = prev_pair.right.end - self.min_overlap - 1 if prev_pair else self.max_gap

            # Last region if less than one amplicon length remaining
            if prev_pair:
                if (len(self.primary_reference) - prev_pair.right.end) < self.amplicon_length:
                    is_last_region = True
                    logger.debug('Region {}: is last region'.format(region_num))

            # Log limits
            logger.debug('Region {}: forward primer limits {}:{}'.format(region_num, left_primer_left_limit, left_primer_right_limit))

            # Find primers or handle no suitable error
            try:
                region = self._find_primers(region_num, left_primer_left_limit, left_primer_right_limit, is_last_region)
                regions.append(region)
            except NoSuitableError:
                logger.debug('Region {}: no suitable primer error'.format(region_num))
                break

            # Handle the end
            if is_last_region:
                logger.debug('Region {}: ending normally'.format(region_num))
                break

            # Report scores and alignments
            for i in range(0, len(self.references)):
                # Don't display alignment to reference
                logger.debug(regions[-1].candidate_pairs[0].left.alignments[i].formatted_alignment)
            logger.debug('Identities for sorted left candidates: ' + ','.join(['%.2f' %each.left.percent_identity for each in regions[-1].candidate_pairs]))
            logger.debug('Left start for sorted candidates: ' + ','.join(['%i' %each.left.start for each in regions[-1].candidate_pairs]))
            logger.debug('Left end for sorted candidates: ' + ','.join(['%i' %each.left.end for each in regions[-1].candidate_pairs]))
            logger.debug('Left length for sorted candidates: ' + ','.join(['%i' %each.left.length for each in regions[-1].candidate_pairs]))

            for i in range(0, len(self.references)):
                logger.debug(regions[-1].candidate_pairs[0].right.alignments[i].formatted_alignment)
            logger.debug('Identities for sorted right candidates: ' + ','.join(['%.2f' %each.right.percent_identity for each in regions[-1].candidate_pairs]))
            logger.debug('Right start for sorted candidates: ' + ','.join(['%i' %each.right.start for each in regions[-1].candidate_pairs]))
            logger.debug('Right end for sorted candidates: ' + ','.join(['%i' %each.right.end for each in regions[-1].candidate_pairs]))
            logger.debug('Right length for sorted candidates: ' + ','.join(['%i' %each.right.length for each in regions[-1].candidate_pairs]))

            logger.debug('Totals for sorted pairs: ' + ','.join(['%.2f' %each.mean_percent_identity for each in regions[-1].candidate_pairs]))

            if len(regions) > 1:
            # Remember, results now include this one, so -2 is the other pool
                trimmed_overlap = regions[-2].candidate_pairs[0].right.end - regions[-1].candidate_pairs[0].left.end - 1
                logger.info("Region %i: highest scoring product %i:%i, length %i, trimmed overlap %i" % (region_num, regions[-1].candidate_pairs[0].left.start, regions[-1].candidate_pairs[0].right.start, regions[-1].candidate_pairs[0].product_length, trimmed_overlap))
            else:
                logger.info("Region %i: highest scoring product %i:%i, length %i" % (region_num, regions[-1].candidate_pairs[0].left.start, regions[-1].candidate_pairs[0].right.start, regions[-1].candidate_pairs[0].product_length))

        # Return regions
        self.regions = regions

    def write_bed(self, path='./'):
        logger.info('Writing BED')
        filepath = os.path.join(path, '{}.bed'.format(self.prefix))
        with open(filepath, 'w') as bedhandle:
            for r in self.regions:
                print >>bedhandle, '\t'.join(map(
                    str, [self.primary_reference.id, r.top_pair.left.start, r.top_pair.left.end, r.top_pair.left.name, r.pool]))
                print >>bedhandle, '\t'.join(map(str, [self.primary_reference.id, r.top_pair.right.end,
                                                       r.top_pair.right.start, r.top_pair.right.name, r.pool]))

    def write_tsv(self, path='./'):
        logger.info('Writing TSV')
        filepath = os.path.join(path, '{}.tsv'.format(self.prefix))
        with open(filepath, 'w') as tsvhandle:
            print >>tsvhandle, '\t'.join(
                ['name', 'seq', 'length', '%gc', 'tm (use 65)'])
            for r in self.regions:
                left = r.top_pair.left
                right = r.top_pair.right
                print >>tsvhandle, '\t'.join(
                    map(str, [left.name, left.seq, left.length, '%.2f' %left.gc, '%.2f' %left.tm]))
                print >>tsvhandle, '\t'.join(
                    map(str, [right.name, right.seq, right.length, '%.2f' %right.gc, '%.2f' %right.tm]))
                if r.alternates:
                    for alt in r.alternates:
                        print >>tsvhandle, '\t'.join(map(str, [alt.name, alt.seq, alt.length, '%.2f' %alt.gc, '%.2f' %alt.tm]))

    def write_pickle(self, path='./'):
        logger.info('Writing pickles')
        filepath = os.path.join(path, '{}.pickle'.format(self.prefix))
        with open(filepath, 'wb') as pickleobj:
            pickle.dump(self.regions, pickleobj)

    def write_refs(self, path='./'):
        logger.info('Writing references')
        filepath = os.path.join(path, '{}.fasta'.format(self.prefix))
        with open(filepath, 'w') as refhandle:
            SeqIO.write(self.references, filepath, 'fasta')

    def write_schemadelica_plot(self, path='./'):
        logger.info('Writing plot')
        gd_diagram = GenomeDiagram.Diagram("Primer Scheme", track_size=1)
        scale_track = GenomeDiagram.Track(
            name='scale', scale=True, scale_fontsize=10, scale_largetick_interval=1000, height=0.1)
        gd_diagram.add_track(scale_track, 2)

        primer_feature_set_1 = GenomeDiagram.FeatureSet()
        primer_feature_set_2 = GenomeDiagram.FeatureSet()

        for r in self.regions:
            cols1 = [self.primary_reference.id, r.top_pair.left.start,
                     r.top_pair.left.end, r.top_pair.left.name, r.pool]
            cols2 = [self.primary_reference.id, r.top_pair.right.end,
                     r.top_pair.right.start, r.top_pair.right.name, r.pool]
            region = str(r.region_num)
            fwd_feature = SeqFeature(FeatureLocation(
                int(cols1[1]), int(cols1[2]), strand=0))
            rev_feature = SeqFeature(FeatureLocation(
                int(cols2[1]), int(cols2[2]), strand=0))
            region_feature = SeqFeature(FeatureLocation(
                int(cols1[1]), int(cols2[2]), strand=0))
            if int(region) % 2 == 0:
                primer_feature_set_1.add_feature(region_feature, color=colors.palevioletred,
                                                 name=region, label=True, label_size=10, label_position="middle", label_angle=0)
                primer_feature_set_1.add_feature(
                    fwd_feature, color=colors.red, name=region, label=False)
                primer_feature_set_1.add_feature(
                    rev_feature, color=colors.red, name=region, label=False)
            else:
                primer_feature_set_2.add_feature(region_feature, color=colors.palevioletred,
                                                 name=region, label=True, label_size=10, label_position="middle", label_angle=0)
                primer_feature_set_2.add_feature(
                    fwd_feature, color=colors.red, name=region, label=False)
                primer_feature_set_2.add_feature(
                    rev_feature, color=colors.red, name=region, label=False)

        primer_track = GenomeDiagram.Track(name="Annotated Features", height=0.1)
        primer_track.add_set(primer_feature_set_1)
        gd_diagram.add_track(primer_track, 4)

        primer_track = GenomeDiagram.Track(name="Annotated Features", height=0.1)
        primer_track.add_set(primer_feature_set_2)
        gd_diagram.add_track(primer_track, 6)

        rows = max(2, int(round(len(self.primary_reference) / 10000.0)))
        gd_diagram.draw(format='linear', pagesize=(300 * rows, 200 * rows),
                        fragments=rows, start=0, end=len(self.primary_reference))

        pdf_filepath = os.path.join(path, '{}.pdf'.format(self.prefix))
        svg_filepath = os.path.join(path, '{}.svg'.format(self.prefix))
        gd_diagram.write(pdf_filepath, 'PDF', dpi=300)
        gd_diagram.write(svg_filepath, 'SVG', dpi=300)

    def _find_primers(self, region_num, left_primer_left_limit, left_primer_right_limit, is_last_region):
        """
        Find primers for a given region.

        Return a list of Region objects containing candidate
        primer pairs sorted by an alignment score summed over all references.
        """

        # Calculate where to slice the reference
        if region_num == 1:
            chunk_start = 0
            chunk_end = int((1 + self.max_variation / 2) * self.amplicon_length)
        elif is_last_region:
            # Last time work backwards
            chunk_start = int(len(self.primary_reference) - ((1 + self.max_variation / 2) * self.amplicon_length))
            chunk_end = len(self.primary_reference)
        else:
            # right limit - min overlap - diff max min product length - max primer length
            chunk_start = int(left_primer_right_limit - (self.max_variation * self.amplicon_length) - settings.global_args['PRIMER_MAX_SIZE'])
            chunk_end = int(chunk_start + ((1 + self.max_variation/2) * self.amplicon_length))
        _chunk_start = chunk_start
        _chunk_end = chunk_end

        # Primer3 setup
        p3_global_args = settings.global_args
        p3_seq_args = settings.seq_args
        p3_global_args['PRIMER_PRODUCT_SIZE_RANGE'] = [
            [int(self.amplicon_length * (1 - self.max_variation / 2)), int(self.amplicon_length * (1 + self.max_variation / 2))]]
        p3_global_args['PRIMER_NUM_RETURN'] = self.max_candidates

        # Run primer3 until primers are found
        hit_left_limit = False
        while True:
            # Slice primary reference
            seq = str(self.primary_reference.seq[chunk_start:chunk_end])
            p3_seq_args['SEQUENCE_TEMPLATE'] = seq
            p3_seq_args['SEQUENCE_INCLUDED_REGION'] = [0, len(seq) - 1]
            logger.debug("Region %i: reference chunk %i:%i, length %i" %(region_num, chunk_start, chunk_end, len(seq)))
            primer3_output = primer3.bindings.designPrimers(p3_seq_args, p3_global_args)

            candidate_pairs = []
            for cand_num in range(self.max_candidates):
                lenkey = 'PRIMER_LEFT_%i' % (cand_num)
                left_name = '%s_%i_%s' % (self.prefix, region_num, 'LEFT')
                right_name = '%s_%i_%s' % (self.prefix, region_num, 'RIGHT')
                if lenkey not in primer3_output:
                    break

                left_seq = str(primer3_output['PRIMER_LEFT_%i_SEQUENCE' % (cand_num)])
                right_seq = str(primer3_output['PRIMER_RIGHT_%i_SEQUENCE' % (cand_num)])

                left_start = int(primer3_output['PRIMER_LEFT_%i' % (cand_num)][0] + chunk_start)
                right_start = int(primer3_output['PRIMER_RIGHT_%i' % (cand_num)][0] + chunk_start + 1)

                left = CandidatePrimer('LEFT', left_name, left_seq, left_start, self.references)
                right = CandidatePrimer('RIGHT', right_name, right_seq, right_start, self.references)

                candidate_pairs.append(CandidatePrimerPair(left, right))

            set_left = set(pair.left.seq for pair in candidate_pairs)
            set_right = set(pair.right.seq for pair in candidate_pairs)

            logger.info("Region %i: current position returned %i left and %i right unique" %(region_num, len(set_left), len(set_right)))

            if len(set_left) > 2 and len(set_right) > 2:
                return Region(region_num, chunk_start, candidate_pairs, self.references, self.prefix, self.max_alts)

            # Move right if first region or to open gap
            if region_num == 1 or hit_left_limit:
                logger.debug("Region %i: stepping right, position %i" %(region_num, chunk_start))
                chunk_start += self.step_size
                chunk_end += self.step_size
                # Hit end of regerence
                if chunk_end > len(self.primary_reference):
                    logger.debug("Region %i: hit right limit %i" %(region_num, len(self.primary_reference)))
                    raise NoSuitableError("No suitable primers in region")
            else:
                # Move left for all other regions
                logger.debug("Region %i: stepping left, position %i, limit %s" %(region_num, chunk_start, left_primer_left_limit))
                chunk_start -= self.step_size
                chunk_end -= self.step_size
                if chunk_start <= left_primer_left_limit:
                    # Switch direction to open gap
                    logger.debug("Region %i: hit left limit" %(region_num))
                    chunk_start = _chunk_start
                    chunk_end = _chunk_end
                    hit_left_limit = True
