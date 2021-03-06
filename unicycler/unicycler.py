#!/usr/bin/env python3
"""
Copyright 2017 Ryan Wick (rrwick@gmail.com)
https://github.com/rrwick/Unicycler

This module contains the main script for the Unicycler assembler. It is executed when a user runs
`unicycler` (after installation) or `unicycler-runner.py`.

This file is part of Unicycler. Unicycler is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version. Unicycler is distributed in
the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details. You should have received a copy of the GNU General Public License along with Unicycler. If
not, see <http://www.gnu.org/licenses/>.
"""

import argparse
import os
import sys
import shutil
import copy
import random
import itertools
from .assembly_graph import AssemblyGraph
from .assembly_graph_copy_depth import determine_copy_depth
from .bridge_long_read import create_long_read_bridges
from .bridge_spades_contig import create_spades_contig_bridges
from .bridge_loop_unroll import create_loop_unrolling_bridges
from .misc import int_to_str, float_to_str, quit_with_error, get_percentile, bold, \
    check_input_files, MyHelpFormatter, print_table, get_ascii_art, \
    get_default_thread_count, spades_path_and_version, makeblastdb_path_and_version, \
    tblastn_path_and_version, bowtie2_build_path_and_version, bowtie2_path_and_version, \
    samtools_path_and_version, java_path_and_version, pilon_path_and_version
from .spades_func import get_best_spades_graph
from .blast_func import find_start_gene, CannotFindStart
from .unicycler_align import add_aligning_arguments, fix_up_arguments, AlignmentScoringScheme, \
    semi_global_align_long_reads, load_references, load_long_reads, load_sam_alignments, \
    print_alignment_summary_table
from .pilon_func import polish_with_pilon, CannotPolish
from . import log
from . import settings
from .version import __version__


def main():
    """
    Script execution starts here.
    """
    # Fix the random seed so the program produces the same output every time it's run.
    random.seed(0)

    full_command = ' '.join(('"' + x + '"' if ' ' in x else x) for x in sys.argv)
    args = get_arguments()
    out_dir_message = make_output_directory(args.out, args.verbosity)

    check_input_files(args)
    print_intro_message(args, full_command, out_dir_message)
    check_dependencies(args)

    # Files are numbered in chronological order
    counter = itertools.count(start=1)
    unbridged_graph_filename = gfa_path(args.out, next(counter), 'unbridged_graph')

    # Produce a SPAdes assembly graph with a k-mer that balances contig length and connectivity.
    if os.path.isfile(unbridged_graph_filename):
        log.log('\nUnbridged graph already exists. Will use this graph instead of running '
                'SPAdes:\n  ' + unbridged_graph_filename)
        unbridged_graph = AssemblyGraph(unbridged_graph_filename, None)
    else:
        unbridged_graph = get_best_spades_graph(args.short1, args.short2, args.unpaired,
                                                args.out, settings.READ_DEPTH_FILTER,
                                                args.verbosity, args.spades_path, args.threads,
                                                args.keep, args.kmer_count, args.min_kmer_frac,
                                                args.max_kmer_frac, args.no_correct,
                                                args.linear_seqs)

    # Determine copy number and get single copy segments.
    single_copy_segments = get_single_copy_segments(unbridged_graph, 0)
    if args.keep > 0:
        unbridged_graph.save_to_gfa(unbridged_graph_filename, save_copy_depth_info=True)

    # Make an initial set of bridges using the SPAdes contig paths. This step is skipped when
    # using conservative bridging mode (in that case we don't trust SPAdes contig paths at all).
    if args.mode == 0:
        bridges = []
        graph = copy.deepcopy(unbridged_graph)
    else:
        log.log_section_header('Bridging graph with SPAdes contigs')
        bridges = create_spades_contig_bridges(unbridged_graph, single_copy_segments)
        bridges += create_loop_unrolling_bridges(unbridged_graph)
        graph = copy.deepcopy(unbridged_graph)
        if not bridges:
            log.log('none found', 1)
        else:
            seg_nums_used_in_bridges = graph.apply_bridges(bridges, args.verbosity,
                                                           args.min_bridge_qual, unbridged_graph)
            log.log('')
            if args.keep > 0:
                graph.save_to_gfa(gfa_path(args.out, next(counter), 'short_read_bridges_applied'),
                                  save_seg_type_info=True, save_copy_depth_info=True)

            graph.clean_up_after_bridging_1(single_copy_segments, seg_nums_used_in_bridges)
            graph.clean_up_after_bridging_2(seg_nums_used_in_bridges, args.min_component_size,
                                            args.min_dead_end_size, unbridged_graph,
                                            single_copy_segments)
            if args.keep > 2:
                graph.save_to_gfa(gfa_path(args.out, next(counter), 'cleaned'),
                                  save_seg_type_info=True, save_copy_depth_info=True)
            graph.merge_all_possible(single_copy_segments, args.mode)
            if args.keep > 2:
                graph.save_to_gfa(gfa_path(args.out, next(counter), 'merged'))

    # Prepare for long read alignment.
    alignment_dir = os.path.join(args.out, 'read_alignment')
    graph_fasta = os.path.join(alignment_dir, 'all_segments.fasta')
    single_copy_segments_fasta = os.path.join(alignment_dir, 'single_copy_segments.fasta')
    single_copy_segment_names = set(str(x.number) for x in single_copy_segments)
    alignments_sam = os.path.join(alignment_dir, 'long_read_alignments.sam')
    scoring_scheme = AlignmentScoringScheme(args.scores)
    min_alignment_length = unbridged_graph.overlap * \
        settings.MIN_ALIGNMENT_LENGTH_RELATIVE_TO_GRAPH_OVERLAP
    if args.long:
        if not os.path.exists(alignment_dir):
            os.makedirs(alignment_dir)
        unbridged_graph.save_to_fasta(graph_fasta)
        unbridged_graph.save_specific_segments_to_fasta(single_copy_segments_fasta,
                                                        single_copy_segments)

    # If all long reads are available now, then we do the entire process in one pass.
    if args.long:
        references = load_references(graph_fasta, section_header='Loading single copy segments')
        reference_dict = {x.name: x for x in references}
        read_dict, read_names, long_read_filename = load_long_reads(args.long)

        # Load existing alignments if available.
        if os.path.isfile(alignments_sam) and sam_references_match(alignments_sam, unbridged_graph):
            log.log('\nSAM file already exists. Will use these alignments instead of conducting '
                  'a new alignment:')
            log.log('  ' + alignments_sam)
            alignments = load_sam_alignments(alignments_sam, read_dict, reference_dict,
                                             scoring_scheme)
            for alignment in alignments:
                read_dict[alignment.read.name].alignments.append(alignment)
            print_alignment_summary_table(read_dict, args.verbosity, False)

        # Conduct the alignment if an existing SAM is not available.
        else:
            alignments_1_sam = os.path.join(alignment_dir, 'long_read_alignments_pass_1.sam')
            alignments_1_in_progress = alignments_1_sam + '.incomplete'
            alignments_2_sam = os.path.join(alignment_dir, 'long_read_alignments_pass_2.sam')
            alignments_2_in_progress = alignments_2_sam + '.incomplete'

            allowed_overlap = int(round(unbridged_graph.overlap *
                                        settings.ALLOWED_ALIGNMENT_OVERLAP))
            low_score_threshold = [args.low_score]
            semi_global_align_long_reads(references, graph_fasta, read_dict, read_names,
                                         long_read_filename, args.threads, scoring_scheme,
                                         low_score_threshold, False, min_alignment_length,
                                         alignments_1_in_progress, full_command, allowed_overlap,
                                         0, args.contamination, args.verbosity,
                                         stdout_header='Aligning reads (first pass)',
                                         single_copy_segment_names=single_copy_segment_names)
            shutil.move(alignments_1_in_progress, alignments_1_sam)

            # Some reads are aligned again with a more sensitive mode: those with multiple
            # alignments or unaligned parts.
            retry_read_names = [x.name for x in read_dict.values() if
                                (x.get_fraction_aligned() < settings.MIN_READ_FRACTION_ALIGNED or
                                 len(x.alignments) > 1) and x.get_length() >= min_alignment_length]
            if retry_read_names:
                semi_global_align_long_reads(references, single_copy_segments_fasta, read_dict,
                                             retry_read_names, long_read_filename,
                                             args.threads, scoring_scheme, low_score_threshold,
                                             False, min_alignment_length, alignments_2_in_progress,
                                             full_command, allowed_overlap, 3,
                                             args.contamination, args.verbosity,
                                             stdout_header='Aligning reads (second pass)',
                                             display_low_score=False,
                                             single_copy_segment_names=single_copy_segment_names)
                shutil.move(alignments_2_in_progress, alignments_2_sam)

                # Now we have to put together a final SAM file. If a read is in the second pass,
                # then we use the alignments from that SAM. Otherwise we take the alignments from
                # the first SAM.
                retry_read_names = set(retry_read_names)
                with open(alignments_sam, 'wt') as alignments_file:
                    with open(alignments_1_sam, 'rt') as alignments_1:
                        for line in alignments_1:
                            if line.startswith('@'):
                                alignments_file.write(line)
                            else:
                                read_name = line.split('\t', 1)[0]
                                if read_name not in retry_read_names:
                                    alignments_file.write(line)
                    with open(alignments_2_sam, 'rt') as alignments_2:
                        for line in alignments_2:
                            if not line.startswith('@'):
                                alignments_file.write(line)

            # If there are no low fraction reads, we can just rename the first pass SAM to the
            # final SAM.
            else:
                shutil.move(alignments_1_sam, alignments_sam)
            if args.keep < 2:
                shutil.rmtree(alignment_dir)
                log.log('\nDeleting ' + alignment_dir + '/')
            if args.keep < 3 and os.path.isfile(alignments_1_sam):
                os.remove(alignments_1_sam)
            if args.keep < 3 and os.path.isfile(alignments_2_sam):
                os.remove(alignments_2_sam)
            if args.keep < 3 and os.path.isfile(graph_fasta):
                os.remove(graph_fasta)
            if args.keep < 3 and os.path.isfile(single_copy_segments_fasta):
                os.remove(single_copy_segments_fasta)

        # Discard any reads that mostly align to known contamination.
        if args.contamination:
            filtered_read_names = []
            filtered_read_dict = {}
            contaminant_read_count = 0
            for read_name in read_names:
                if read_dict[read_name].mostly_aligns_to_contamination():
                    contaminant_read_count += 1
                else:
                    filtered_read_names.append(read_name)
                    filtered_read_dict[read_name] = read_dict[read_name]
            read_names = filtered_read_names
            read_dict = filtered_read_dict
            log.log('\nDiscarded', contaminant_read_count, 'reads as contamination', 2)

        # Use the long reads which aligned entirely within contigs (which are most likely correct)
        # to determine a minimum score.
        contained_reads = [x for x in read_dict.values() if x.has_one_contained_alignment()]
        contained_scores = []
        for read in contained_reads:
            contained_scores += [x.scaled_score for x in read.alignments]
        min_scaled_score = get_percentile(contained_scores, settings.MIN_SCALED_SCORE_PERCENTILE)

        log.log('\nSetting the minimum scaled score to the ' +
                float_to_str(settings.MIN_SCALED_SCORE_PERCENTILE, 1) +
                'th percentile of full read alignments: ' + float_to_str(min_scaled_score, 2), 2)

        # Do the long read bridging - this is the good part!
        log.log_section_header('Building long read bridges')
        expected_linear_seqs = args.linear_seqs > 0
        bridges = create_long_read_bridges(unbridged_graph, read_dict, read_names,
                                           single_copy_segments, args.verbosity, bridges,
                                           min_scaled_score, args.threads, scoring_scheme,
                                           min_alignment_length, expected_linear_seqs,
                                           args.min_bridge_qual)
        graph = copy.deepcopy(unbridged_graph)
        log.log_section_header('Bridging graph with long reads')
        seg_nums_used_in_bridges = graph.apply_bridges(bridges, args.verbosity,
                                                       args.min_bridge_qual, unbridged_graph)
        if args.keep > 0:
            graph.save_to_gfa(gfa_path(args.out, next(counter), 'long_read_bridges_applied'),
                              save_seg_type_info=True, save_copy_depth_info=True, newline=True)

        graph.clean_up_after_bridging_1(single_copy_segments, seg_nums_used_in_bridges)
        graph.clean_up_after_bridging_2(seg_nums_used_in_bridges, args.min_component_size,
                                        args.min_dead_end_size, unbridged_graph,
                                        single_copy_segments)
        if args.keep > 2:
            log.log('', 2)
            graph.save_to_gfa(gfa_path(args.out, next(counter), 'cleaned'),
                              save_seg_type_info=True, save_copy_depth_info=True)
        graph.merge_all_possible(single_copy_segments, args.mode)
        if args.keep > 2:
            graph.save_to_gfa(gfa_path(args.out, next(counter), 'merged'))

    # Perform a final clean on the graph, including overlap removal.
    graph.final_clean()
    log.log_section_header('Bridged assembly graph')
    graph.print_component_table()
    if args.keep > 0:
        graph.save_to_gfa(gfa_path(args.out, next(counter), 'final_clean'), newline=True)

    # Rotate completed replicons in the graph to a standard starting gene.
    completed_replicons = graph.completed_circular_replicons()
    if not args.no_rotate and len(completed_replicons) > 0:
        log.log_section_header('Rotating completed replicons')

        rotation_result_table = [['Segment', 'Length', 'Depth', 'Starting gene', 'Position',
                                  'Strand', 'Identity', 'Coverage']]
        blast_dir = os.path.join(args.out, 'blast')
        if not os.path.exists(blast_dir):
            os.makedirs(blast_dir)
        completed_replicons = sorted(completed_replicons, reverse=True,
                                     key=lambda x: graph.segments[x].get_length())
        rotation_count = 0
        for completed_replicon in completed_replicons:
            segment = graph.segments[completed_replicon]
            sequence = segment.forward_sequence
            if graph.overlap > 0:
                sequence = sequence[:-graph.overlap]
            depth = segment.depth
            log.log('Segment ' + str(segment.number) + ':', 2)
            rotation_result_row = [str(segment.number), int_to_str(len(sequence)),
                                   float_to_str(depth, 2) + 'x']
            try:
                blast_hit = find_start_gene(sequence, args.start_genes, args.start_gene_id,
                                            args.start_gene_cov, blast_dir, args.makeblastdb_path,
                                            args.tblastn_path, args.threads)
            except CannotFindStart:
                rotation_result_row += ['none found', '', '', '', '']
            else:
                rotation_result_row += [blast_hit.qseqid, int_to_str(blast_hit.start_pos),
                                        'reverse' if blast_hit.flip else 'forward',
                                        '%.1f' % blast_hit.pident + '%',
                                        '%.1f' % blast_hit.query_cov + '%']
                segment.rotate_sequence(blast_hit.start_pos, blast_hit.flip, graph.overlap)
                rotation_count += 1
            rotation_result_table.append(rotation_result_row)

        log.log('', 2)
        print_table(rotation_result_table, alignments='RRRLRLRR', indent=0,
                    sub_colour={'none found': 'red'})
        if rotation_count and args.keep > 0:
            graph.save_to_gfa(gfa_path(args.out, next(counter), 'rotated'), newline=True)
        if args.keep < 3 and os.path.exists(blast_dir):
            shutil.rmtree(blast_dir)

    # Polish the final assembly!
    if not args.no_pilon:
        log.log_section_header('Polishing assembly with Pilon')
        polish_dir = os.path.join(args.out, 'pilon_polish')
        if not os.path.exists(polish_dir):
            os.makedirs(polish_dir)
        starting_dir = os.getcwd()
        try:
            polish_with_pilon(graph, args.bowtie2_path, args.bowtie2_build_path, args.pilon_path,
                              args.java_path, args.samtools_path, args.min_polish_size, polish_dir,
                              args.short1, args.short2, args.threads)
        except CannotPolish as e:
            log.log('Unable to polish assembly using Pilon: ' + e.message)
        else:
            if args.keep > 0:
                graph.save_to_gfa(gfa_path(args.out, next(counter), 'polished'), newline=True)
        os.chdir(starting_dir)
        if args.keep < 3 and os.path.exists(polish_dir):
            shutil.rmtree(polish_dir)

    # Save the final state as both a GFA and FASTA file.
    log.log_section_header('Complete')
    graph.save_to_gfa(os.path.join(args.out, 'assembly.gfa'))
    graph.save_to_fasta(os.path.join(args.out, 'assembly.fasta'), min_length=args.min_fasta_length)
    log.log('')


def get_arguments():
    """
    Parse the command line arguments.
    """
    description = bold('Unicycler: a hybrid assembly pipeline for bacterial genomes')
    this_script_dir = os.path.dirname(os.path.realpath(__file__))

    if '--helpall' in sys.argv or '--allhelp' in sys.argv or '--all_help' in sys.argv:
        sys.argv.append('--help_all')
    show_all_args = '--help_all' in sys.argv

    # Show the ASCII art if the terminal is wide enough for it.
    terminal_width = shutil.get_terminal_size().columns
    if terminal_width >= 70:
        full_description = 'R|' + get_ascii_art() + '\n\n' + description
    else:
        full_description = description
    parser = argparse.ArgumentParser(description=full_description, formatter_class=MyHelpFormatter,
                                     add_help=False)

    # Help options
    help_group = parser.add_argument_group('Help')
    help_group.add_argument('-h', '--help', action='help',
                            help='Show this help message and exit')
    help_group.add_argument('--help_all', action='help',
                            help='Show a help message with all program options')
    help_group.add_argument('--version', action='version', version='Unicycler v' + __version__,
                            help="Show Unicycler's version number")

    # Short read input options
    input_group = parser.add_argument_group('Input')
    input_group.add_argument('-1', '--short1', required=True,
                             help='FASTQ file of first short reads in each pair (required)')
    input_group.add_argument('-2', '--short2', required=True,
                             help='FASTQ file of second short reads in each pair (required)')
    input_group.add_argument('-s', '--unpaired', required=False,
                             help='FASTQ file of unpaired short reads (optional)')

    # Long read input options
    input_group.add_argument('-l', '--long', required=False,
                             help='FASTQ or FASTA file of long reads (optional)')

    # Output options
    output_group = parser.add_argument_group('Output')
    output_group.add_argument('-o', '--out', required=True,
                              help='Output directory (required)')
    output_group.add_argument('--verbosity', type=int, required=False, default=1,
                              help='R|Level of stdout and log file information (default: 1)\n  '
                                   '0 = no stdout, 1 = basic progress indicators, '
                                   '2 = extra info, 3 = debugging info')
    output_group.add_argument('--min_fasta_length', type=int, required=False, default=1,
                              help='Exclude contigs from the FASTA file which are shorter than '
                                   'this length (default: 1)')
    output_group.add_argument('--keep', type=int, default=1,
                              help='R|Level of file retention (default: 1)\n  '
                                   '0 = only keep final files: assembly (FASTA, GFA and log), '
                                   '1 = also save graphs at main checkpoints, '
                                   '2 = also keep SAM (enables fast rerun in different mode), '
                                   '3 = keep all temp files and save all graphs (for debugging)')

    other_group = parser.add_argument_group('Other')
    other_group.add_argument('-t', '--threads', type=int, required=False,
                             default=get_default_thread_count(),
                             help='Number of threads used')
    other_group.add_argument('--mode', choices=['conservative', 'normal', 'bold'], default='normal',
                             help='B|Bridging mode (default: normal)\n'
                                  '  conservative = smaller contigs, lowest misassembly rate\n'
                                  '  normal = moderate contig size and misassembly rate\n'
                                  '  bold = longest contigs, higher misassembly rate')
    other_group.add_argument('--min_bridge_qual', type=float,
                             help='R|Do not apply bridges with a quality below this value\n'
                                  '  conservative mode default: ' +
                                  str(settings.CONSERVATIVE_MIN_BRIDGE_QUAL) + '\n'
                                  '  normal mode default: ' +
                                  str(settings.NORMAL_MIN_BRIDGE_QUAL) + '\n'
                                  '  bold mode default: ' +
                                  str(settings.BOLD_MIN_BRIDGE_QUAL)
                                  if show_all_args else argparse.SUPPRESS)
    other_group.add_argument('--linear_seqs', type=int, required=False, default=0,
                             help='The expected number of linear (i.e. non-circular) sequences in '
                                  'the underlying sequence')

    # SPAdes assembly options
    spades_group = parser.add_argument_group('SPAdes assembly',
                                             'These options control the short read SPAdes '
                                             'assembly at the beginning of the Unicycler pipeline.'
                                             if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--spades_path', type=str, default='spades.py',
                              help='Path to the SPAdes executable'
                                   if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--no_correct', action='store_true',
                              help='Skip SPAdes error correction step (default: conduct SPAdes '
                                   'error correction)'
                                   if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--min_kmer_frac', type=float, default=0.2,
                              help='Lowest k-mer size for SPAdes assembly, expressed as a '
                                   'fraction of the read length'
                                   if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--max_kmer_frac', type=float, default=0.95,
                              help='Highest k-mer size for SPAdes assembly, expressed as a '
                                   'fraction of the read length'
                                   if show_all_args else argparse.SUPPRESS)
    spades_group.add_argument('--kmer_count', type=int, default=10,
                              help='Number of k-mer steps to use in SPAdes assembly'
                                   if show_all_args else argparse.SUPPRESS)

    # Rotation options
    rotation_group = parser.add_argument_group('Assembly rotation',
                                               'These options control the rotation of completed '
                                               'circular sequence near the end of the Unicycler '
                                               'pipeline.'
                                               if show_all_args else argparse.SUPPRESS)
    rotation_group.add_argument('--no_rotate', action='store_true',
                                help='Do not rotate completed replicons to start at a standard '
                                     'gene (default: completed replicons are rotated)'
                                     if show_all_args else argparse.SUPPRESS)
    rotation_group.add_argument('--start_genes', type=str,
                                default=os.path.join(this_script_dir, 'gene_data',
                                                     'start_genes.fasta'),
                                help='FASTA file of genes for start point of rotated replicons '
                                     '(default: start_genes.fasta)'
                                     if show_all_args else argparse.SUPPRESS)
    rotation_group.add_argument('--start_gene_id', type=float, default=90.0,
                                help='The minimum required BLAST percent identity for a start gene '
                                     'search'
                                     if show_all_args else argparse.SUPPRESS)
    rotation_group.add_argument('--start_gene_cov', type=float, default=95.0,
                                help='The minimum required BLAST percent coverage for a start gene '
                                     'search'
                                     if show_all_args else argparse.SUPPRESS)
    rotation_group.add_argument('--makeblastdb_path', type=str, default='makeblastdb',
                                help='Path to the makeblastdb executable'
                                     if show_all_args else argparse.SUPPRESS)
    rotation_group.add_argument('--tblastn_path', type=str, default='tblastn',
                                help='Path to the tblastn executable'
                                     if show_all_args else argparse.SUPPRESS)

    # Polishing options
    polish_group = parser.add_argument_group('Pilon polishing',
                                             'These options control the final assembly polish '
                                             'using Pilon at the end of the Unicycler pipeline.'
                                             if show_all_args else argparse.SUPPRESS)
    polish_group.add_argument('--no_pilon', action='store_true',
                              help='Do not use Pilon to polish the final assembly (default: Pilon '
                                   'is used)'
                                   if show_all_args else argparse.SUPPRESS)
    polish_group.add_argument('--bowtie2_path', type=str, default='bowtie2',
                              help='Path to the bowtie2 executable'
                                   if show_all_args else argparse.SUPPRESS)
    polish_group.add_argument('--bowtie2_build_path', type=str, default='bowtie2-build',
                              help='Path to the bowtie2_build executable'
                                   if show_all_args else argparse.SUPPRESS)
    polish_group.add_argument('--samtools_path', type=str, default='samtools',
                              help='Path to the samtools executable'
                                   if show_all_args else argparse.SUPPRESS)
    polish_group.add_argument('--pilon_path', type=str, default='pilon',
                              help='Path to a Pilon executable or the Pilon Java archive file'
                                   if show_all_args else argparse.SUPPRESS)
    polish_group.add_argument('--java_path', type=str, default='java',
                              help='Path to the java executable'
                                   if show_all_args else argparse.SUPPRESS)
    polish_group.add_argument('--min_polish_size', type=int, default=10000,
                              help='Contigs shorter than this value (bp) will not be polished '
                                   'using Pilon'
                                   if show_all_args else argparse.SUPPRESS)

    # Graph cleaning options
    cleaning_group = parser.add_argument_group('Graph cleaning',
                                               'These options control the removal of small '
                                               'leftover sequences after bridging is complete.'
                                               if show_all_args else argparse.SUPPRESS)
    cleaning_group.add_argument('--min_component_size', type=int, default=1000,
                                help='Unbridged graph components smaller than this size (bp) will '
                                     'be removed from the final graph'
                                     if show_all_args else argparse.SUPPRESS)
    cleaning_group.add_argument('--min_dead_end_size', type=int, default=1000,
                                help='Graph dead ends smaller than this size (bp) will be removed '
                                     'from the final graph'
                                     if show_all_args else argparse.SUPPRESS)

    # Add the arguments for the aligner, but suppress the help text.
    align_group = parser.add_argument_group('Long read alignment',
                                            'These options control the alignment of long reads to '
                                            'the assembly graph.'
                                            if show_all_args else argparse.SUPPRESS)
    add_aligning_arguments(align_group, show_all_args)

    args = parser.parse_args()
    fix_up_arguments(args)

    if args.keep < 0 or args.keep > 3:
        quit_with_error('--keep must be between 0 and 3 (inclusive)')

    if args.verbosity < 0 or args.verbosity > 3:
        quit_with_error('--verbosity must be between 0 and 3 (inclusive)')

    if args.threads <= 0:
        quit_with_error('--threads must be at least 1')

    # Set up bridging mode related stuff.
    user_set_bridge_qual = args.min_bridge_qual is not None
    if args.mode == 'conservative':
        args.mode = 0
        if not user_set_bridge_qual:
            args.min_bridge_qual = settings.CONSERVATIVE_MIN_BRIDGE_QUAL
    elif args.mode == 'bold':
        args.mode = 2
        if not user_set_bridge_qual:
            args.min_bridge_qual = settings.BOLD_MIN_BRIDGE_QUAL
    else:  # normal
        args.mode = 1
        if not user_set_bridge_qual:
            args.min_bridge_qual = settings.NORMAL_MIN_BRIDGE_QUAL

    # Change some arguments to full paths.
    args.out = os.path.abspath(args.out)
    args.short1 = os.path.abspath(args.short1)
    args.short2 = os.path.abspath(args.short2)
    if args.unpaired:
        args.unpaired = os.path.abspath(args.unpaired)
    if args.long:
        args.long = os.path.abspath(args.long)

    # Create an initial logger which doesn't have an output file.
    log.logger = log.Log(None, args.verbosity)

    return args


def make_output_directory(out_dir, verbosity):
    """
    Creates the output directory, if it doesn't already exist.
    """
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
        message = 'Making output directory:'
    elif os.listdir(out_dir):
        message = 'The output directory already exists and files may be reused or overwritten:'
    else:  # directory exists but is empty
        message = 'The output directory already exists:'
    message += '\n  ' + out_dir

    # Now that the output directory exists, we can make our logger store all output in a log file.
    log.logger = log.Log(os.path.join(out_dir, 'unicycler.log'), stdout_verbosity_level=verbosity)

    # The message is returned so it can be logged in the 'Starting Unicycler' section.
    return message


def get_single_copy_segments(graph, min_single_copy_length):
    """
    Returns a list of the graph segments determined to be single copy.
    """
    log.log_section_header('Finding single copy segments')
    determine_copy_depth(graph)
    single_copy_segments = [x for x in graph.get_single_copy_segments()
                            if x.get_length() >= min_single_copy_length]
    log.log('', 2)
    total_single_copy_length = sum([x.get_length() for x in single_copy_segments])
    log.log(int_to_str(len(single_copy_segments)) +
          ' single copy segments (' + int_to_str(total_single_copy_length) + ' bp) out of ' +
          int_to_str(len(graph.segments)) +
          ' total segments (' + int_to_str(graph.get_total_length()) + ' bp)')

    return single_copy_segments


def sam_references_match(sam_filename, assembly_graph):
    """
    Returns True if the references in the SAM header exactly match the graph segment numbers.
    """
    sam_file = open(sam_filename, 'rt')
    ref_numbers_in_sam = []
    for line in sam_file:
        if not line.startswith('@'):
            break
        if not line.startswith('@SQ'):
            continue
        line_parts = line.strip().split()
        if len(line_parts) < 2:
            continue
        ref_name_parts = line_parts[1].split(':')
        if len(ref_name_parts) < 2:
            continue
        try:
            ref_numbers_in_sam.append(int(ref_name_parts[1]))
        except ValueError:
            pass

    ref_numbers_in_sam = sorted(ref_numbers_in_sam)
    seg_numbers_in_graph = sorted(assembly_graph.segments.keys())
    return ref_numbers_in_sam == seg_numbers_in_graph


def print_intro_message(args, full_command, out_dir_message):
    """
    Prints a message at the start of the program's execution.
    """
    log.log_section_header('Starting Unicycler', single_newline=True)
    log.log('Command: ' + bold(full_command))
    log.log('')
    log.log('Using ' + str(args.threads) + ' thread' + ('' if args.threads == 1 else 's'))
    log.log('')
    log.log(out_dir_message)
    log.log('', 2)
    if args.mode == 0:
        log.log('Bridging mode: conservative', 2)
        if args.min_bridge_qual == settings.CONSERVATIVE_MIN_BRIDGE_QUAL:
            log.log('  using default conservative bridge quality cutoff: ', 2, end='')
        else:
            log.log('  using user-specified bridge quality cutoff: ', 2, end='')
    elif args.mode == 1:
        log.log('Bridging mode: normal', 2)
        if args.min_bridge_qual == settings.NORMAL_MIN_BRIDGE_QUAL:
            log.log('  using default normal bridge quality cutoff: ', 2, end='')
        else:
            log.log('  using user-specified bridge quality cutoff: ', 2, end='')
    else:  # args.mode == 2
        log.log('Bridging mode: bold', 2)
        if args.min_bridge_qual == settings.BOLD_MIN_BRIDGE_QUAL:
            log.log('  using default bold bridge quality cutoff: ', 2, end='')
        else:
            log.log('  using user-specified bridge quality cutoff: ', 2, end='')
    log.log(float_to_str(args.min_bridge_qual, 2), 2)


def check_dependencies(args):
    """
    This function prints a table of Unicycler's dependencies and checks their version number.
    It will end the program with an error message if there are any problems.
    """
    log.log('\nDependencies:')
    if args.verbosity <= 1:
        program_table = [['Program', 'Version', 'Status']]
    else:
        program_table = [['Program', 'Version', 'Status', 'Path']]

    spades_path, spades_version, spades_status = spades_path_and_version(args.spades_path)
    spades_row = ['spades.py', spades_version, spades_status]
    if args.verbosity > 1:
        spades_row.append(spades_path)
    program_table.append(spades_row)

    # Rotation dependencies
    if args.no_rotate:
        makeblastdb_path, makeblastdb_version, makeblastdb_status = '', '', 'not used'
        tblastn_path, tblastn_version, tblastn_status = '', '', 'not used'
    else:
        makeblastdb_path, makeblastdb_version, makeblastdb_status = \
            makeblastdb_path_and_version(args.makeblastdb_path)
        tblastn_path, tblastn_version, tblastn_status = tblastn_path_and_version(args.tblastn_path)
    makeblastdb_row = ['makeblastdb', makeblastdb_version, makeblastdb_status]
    tblastn_row = ['tblastn', tblastn_version, tblastn_status]
    if args.verbosity > 1:
        makeblastdb_row.append(makeblastdb_path)
        tblastn_row.append(tblastn_path)
    program_table.append(makeblastdb_row)
    program_table.append(tblastn_row)

    # Polishing dependencies
    if args.no_pilon:
        bowtie2_build_path, bowtie2_build_version, bowtie2_build_status = '', '', 'not used'
        bowtie2_path, bowtie2_version, bowtie2_status = '', '', 'not used'
        samtools_path, samtools_version, samtools_status = '', '', 'not used'
        java_path, java_version, java_status = '', '', 'not used'
        pilon_path, pilon_version, pilon_status = '', '', 'not used'
    else:
        bowtie2_build_path, bowtie2_build_version, bowtie2_build_status = \
            bowtie2_build_path_and_version(args.bowtie2_build_path)
        bowtie2_path, bowtie2_version, bowtie2_status = bowtie2_path_and_version(args.bowtie2_path)
        samtools_path, samtools_version, samtools_status = \
            samtools_path_and_version(args.samtools_path)
        java_path, java_version, java_status = java_path_and_version(args.java_path)
        pilon_path, pilon_version, pilon_status = \
            pilon_path_and_version(args.pilon_path, args.java_path, args)
    bowtie2_build_row = ['bowtie2-build', bowtie2_build_version, bowtie2_build_status]
    bowtie2_row = ['bowtie2', bowtie2_version, bowtie2_status]
    samtools_row = ['samtools', samtools_version, samtools_status]
    java_row = ['java', java_version, java_status]
    pilon_row = ['pilon', pilon_version, pilon_status]
    if args.verbosity > 1:
        bowtie2_build_row.append(bowtie2_build_path)
        bowtie2_row.append(bowtie2_path)
        samtools_row.append(samtools_path)
        java_row.append(java_path)
        pilon_row.append(pilon_path)
    program_table.append(bowtie2_build_row)
    program_table.append(bowtie2_row)
    program_table.append(samtools_row)
    program_table.append(java_row)
    program_table.append(pilon_row)

    row_colours = {}
    for i, row in enumerate(program_table):
        if 'not used' in row:
            row_colours[i] = 'dim'
        elif 'too old' in row or 'not found' in row or 'bad' in row:
            row_colours[i] = 'red'

    print_table(program_table, alignments='LLLL', row_colour=row_colours, max_col_width=60,
                sub_colour={'good': 'green'})

    quit_if_dependency_problem(spades_status, makeblastdb_status, tblastn_status,
                               bowtie2_build_status, bowtie2_status, samtools_status, java_status,
                               pilon_status, args)

def quit_if_dependency_problem(spades_status, makeblastdb_status, tblastn_status,
                               bowtie2_build_status, bowtie2_status, samtools_status, java_status,
                               pilon_status, args):
    if all(x == 'good' or x == 'not used'
           for x in [spades_status, makeblastdb_status, tblastn_status, bowtie2_build_status,
                     bowtie2_status, samtools_status, java_status, pilon_status]):
        return

    log.log('')
    if spades_status == 'not found':
        quit_with_error('could not find SPAdes at ' + args.spades_path)
    if spades_status == 'too old':
        quit_with_error('Unicycler requires SPAdes v3.6.2 or higher')
    if spades_status == 'bad':
        quit_with_error('SPAdes was found but does not produce output (make sure to use '
                        '"spades.py" location, not "spades")')
    if makeblastdb_status == 'not found':
        quit_with_error('could not find makeblastdb - either specify its location using '
                        '--makeblastdb_path or use --no_rotate to remove BLAST dependency')
    if tblastn_status == 'not found':
        quit_with_error('could not find tblastn - either specify its location using '
                        '--tblastn_path or use --no_rotate to remove BLAST dependency')
    if bowtie2_build_status == 'not found':
        quit_with_error('could not find bowtie2-build - either specify its location using '
                        '--bowtie2_build_path or use --no_pilon to remove Bowtie2 dependency')
    if bowtie2_status == 'not found':
        quit_with_error('could not find bowtie2 - either specify its location using '
                        '--bowtie2_path or use --no_pilon to remove Bowtie2 dependency')
    if samtools_status == 'not found':
        quit_with_error('could not find samtools - either specify its location using '
                        '--samtools_path or use --no_pilon to remove Samtools dependency')
    if java_status == 'not found':
        quit_with_error('could not find java - either specify its location using --java_path or '
                        'use --no_pilon to remove Java dependency')
    if pilon_status == 'not found':
        quit_with_error('could not find pilon or pilon*.jar - either specify its location '
                            'using --pilon_path or use --no_pilon to remove Pilon dependency')
    if pilon_status == 'bad':
        quit_with_error('Pilon was found (' + args.pilon_path + ') but does not work - either '
                        'fix it, specify a different location using --pilon_path or use '
                        '--no_pilon to remove Pilon dependency')

    # Code should never get here!
    quit_with_error('Unspecified error with Unicycler dependencies')


def gfa_path(out_dir, file_num, name):
    return os.path.join(out_dir, str(file_num).zfill(3) + '_' + name + '.gfa')
