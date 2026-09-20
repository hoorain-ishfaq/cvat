// Copyright (C) CVAT.ai Corporation
//
// SPDX-License-Identifier: MIT

import React, { useMemo } from 'react';
import {
    BarElement,
    CategoryScale,
    Chart as ChartJS,
    Legend,
    LinearScale,
    Title,
    Tooltip,
    TooltipItem,
} from 'chart.js';
import { Bar } from 'react-chartjs-2';

import { SerializedClassCount } from 'cvat-core-wrapper';

ChartJS.register(CategoryScale, LinearScale, BarElement, Title, Tooltip, Legend);

interface Props {
    classes: SerializedClassCount[];
    totalImages: number;
}

function ClassCountsChart(props: Readonly<Props>): JSX.Element {
    const { classes, totalImages } = props;

    const data = useMemo(() => ({
        labels: classes.map((item) => item.label_name),
        datasets: [{
            label: 'Images',
            data: classes.map((item) => item.image_count),
            // Reuse each label's own colour so the chart matches the annotation UI.
            backgroundColor: classes.map((item) => item.color || '#1890ff'),
            borderColor: classes.map((item) => item.color || '#1890ff'),
            borderWidth: 1,
            borderRadius: 4,
        }],
    }), [classes]);

    const options = useMemo(() => ({
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { display: false },
            title: { display: false },
            tooltip: {
                callbacks: {
                    label: (context: TooltipItem<'bar'>): string => {
                        const count = context.parsed.y;
                        const suffix = count === 1 ? 'image' : 'images';
                        if (totalImages > 0) {
                            const share = Math.round((count / totalImages) * 100);
                            return `${count} ${suffix} (${share}% of the task)`;
                        }
                        return `${count} ${suffix}`;
                    },
                },
            },
        },
        scales: {
            y: {
                beginAtZero: true,
                // Counts are whole images, so hide fractional gridline labels.
                ticks: { precision: 0 },
                title: { display: true, text: 'Images' },
            },
            x: {
                title: { display: true, text: 'Class' },
            },
        },
    }), [totalImages]);

    return (
        <div className='cvat-class-analytics-chart'>
            <Bar data={data} options={options} />
        </div>
    );
}

export default React.memo(ClassCountsChart);
