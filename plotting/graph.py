import math
import argparse

import matplotlib
matplotlib.use('agg')
import matplotlib.pyplot as plt
import matplotlib.style as style
import matplotlib.font_manager as fm
import matplotlib.transforms as transforms
import pandas as pd

# To handle running this script as main, or just import this script
try:
    from .preprocessing import create_subplot
    from .plotSettings import *
except:
    from preprocessing import create_subplot
    from plotSettings import *

def graph(dfs, title, filename):
    
    # figure initialization
    fig, axlist = plt.subplots(figsize=(plot_width, plot_height),nrows=len(dfs))
    color_index = 0

    for i, (metric, df) in enumerate(dfs.items()):
        ax = axlist[i]
        plt.sca(ax)
        style.use('fivethirtyeight')
        graph, color_index = create_subplot(
                ax=ax, 
                xaxis=xaxis, 
                yaxis=yaxis, 
                df=df, 
                ylabel=metric, 
                color_index=color_index, 
                num_colors=len(dfs),
                xlabel=xlabel,
                y_axis_label_size=y_axis_label_size,
                x_axis_label_size=x_axis_label_size,
                legend_size=legend_size, 
                tick_label_size=tick_label_size,
                axis_font=prop,
                legend_font=prop,
                text_opacity=text_opacity,
                xaxis_opacity=xaxis_opacity,
        )

    newXLabel = "Iterations"
    # add axis labels  
    plt.xlabel(newXLabel, 
               fontproperties=prop, 
               fontsize = 24, 
               alpha=text_opacity)

    # transforms the x axis to figure fractions, and leaves y axis in pixels
    xfig_trans = transforms.blended_transform_factory(fig.transFigure, transforms.IdentityTransform())
    yfig_trans = transforms.blended_transform_factory(transforms.IdentityTransform(), fig.transFigure)

    # banner positioning
    banner_y = math.ceil(banner_text_size * 0.6)

    # banner text
    banner = plt.annotate(banner_text, 
             xy=(0.02, banner_y*0.8), 
             xycoords=xfig_trans,
             fontsize = banner_text_size, 
             color = '#FFFFFF', 
             fontname='DecimaMonoPro')

    # banner background height parameters
    pad = 2 # points
    bb = ax.get_window_extent()
    h = bb.height/fig.dpi
    h = h * len(dfs)
    height = ((banner.get_size()+2*pad)/72.)/h
    # height = 0.01

    # banner background
    rect = plt.Rectangle((0,0), 
                         width=1, 
                         height=height,
                         transform=fig.transFigure, 
                         zorder=3,
                         fill=True, 
                         facecolor="grey", 
                         clip_on=False)
    ax.add_patch(rect)

    #transform coordinate of left
    display_left_tuple = xfig_trans.transform((left,0))
    display_left = display_left_tuple[0]

    # shift title
    title_shift_x = math.floor(tick_label_size * 2.6)
    title_shift_x += title_pad_x

    # title
    graph.text(x = display_left - title_shift_x, y = title_pos_y, 
               transform = yfig_trans,
               s = title,
               fontproperties = prop,
               weight = 'bold', 
               fontsize = title_fontsize,
               alpha = text_opacity)

    # subtitle, +1 accounts for font size difference in title and subtitle
    graph.text(x = display_left - title_shift_x + 1, y = subtitle_pos_y, 
               transform = yfig_trans,
               s = '',
               fontproperties=prop,
               fontsize = subtitle_fontsize, 
               alpha = text_opacity)


    # adjust position of subplot in figure
    plt.subplots_adjust(top=top)
    plt.subplots_adjust(bottom=bottom)
    plt.subplots_adjust(left=left)
    plt.subplots_adjust(right=right)

    plt.savefig(filename, dpi=300)

